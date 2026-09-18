"""T2 data layer (phase P0): read master outputs into one day-level long table.

READ-ONLY. Nothing here writes a file or runs a tool; T2 is a preview until
Export. Everything is driven by ``t2_dictionary.yaml``.

Long table (one row per participant / season / device / variable / date):

    participant   real ID (kept internally)
    display_id    what screens and exports show: the real ID, or a pseudonymous
                  ID once ``privacy.pseudonymise`` is switched on
    season        season folder name        season_n   1st, 2nd, 3rd... season
    device, variable, level (day | season), value
    date          calendar date the value belongs to (Expiwell: the real
                  response date, never "day of study")
    night_of      the evening the night starts (see ``timing`` in the dictionary)
    day_of_study  Expiwell's own label, kept for reference only
    valid         that device-day's valid flag, where the device reports one
    partial       the day (or noon-to-noon window) is not fully recorded
    in_window     inside the common window of the selected devices
    window_day    1-based day within that common window
    recording     output file stem (private detail; never published)

Season numbers (``season_n``) come from the dates the recordings actually
cover, not from the folder names, because folder labels are not consistent
(one participant's "Winter 2025" and another's "Winter 2026" can both hold
January 2026 data).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import yaml

from . import pseudo_id
from .config import Config
from .results import _device_folder

DICTIONARY_PATH = Path(__file__).with_name("t2_dictionary.yaml")

#: Fraction of a full day's epochs below which a day counts as partial.
PARTIAL_DAY_FRACTION = 0.9
#: A common window shorter than this is flagged (still usable).
SHORT_WINDOW_DAYS = 7

LONG_COLUMNS = [
    "participant", "display_id", "season", "season_n", "season_key", "season_label",
    "season_type", "season_year", "device", "variable", "level",
    "date", "night_of", "day_of_study", "value", "valid", "partial",
    "in_window", "window_day", "recording",
]

#: Meteorological seasons. Winter spans a year end, so it is named for the year
#: it starts in: January 2026 belongs to "Winter 2025/26".
_SEASON_OF_MONTH = {12: "Winter", 1: "Winter", 2: "Winter",
                    3: "Spring", 4: "Spring", 5: "Spring",
                    6: "Summer", 7: "Summer", 8: "Summer",
                    9: "Autumn", 10: "Autumn", 11: "Autumn"}
SEASON_ORDER = {"Winter": 0, "Spring": 1, "Summer": 2, "Autumn": 3}


def calendar_season(d: date) -> tuple[str, int]:
    """The season of the year a date falls in, and the year that season began."""
    kind = _SEASON_OF_MONTH[d.month]
    year = d.year - 1 if (kind == "Winter" and d.month != 12) else d.year
    return kind, year


def season_key(kind: str, year: int) -> str:
    """Stable id used to line participants up: '2025-winter'."""
    return f"{year}-{kind.lower()}"


def season_label(kind: str, year: int) -> str:
    return f"{kind} {year}/{str(year + 1)[2:]}" if kind == "Winter" else f"{kind} {year}"


def parse_season_key(key: str) -> tuple[str, int]:
    """'2025-winter' -> ('Winter', 2025)."""
    year, _, kind = str(key).partition("-")
    return kind.capitalize(), int(year)


def season_code(kind: str, year: int) -> str:
    """Short form for batch ids: 'win25', 'aut25'."""
    return f"{kind[:3].lower()}{year % 100:02d}"

_DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SEASON_NAME = re.compile(r"^\s*(Winter|Spring|Summer|Autumn)\s+(\d{4})\s*$", re.I)
# Name-only fallback when a season folder has no dated file at all.
_SEASON_MONTH = {"spring": 4, "summer": 7, "autumn": 10, "winter": 12}


# ---------------------------------------------------------------------------
# dictionary
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _load_dictionary_cached(path: str, mtime: float) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    ids = [v["id"] for v in data.get("variables", [])]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate variable ids in the T2 dictionary: {sorted(dupes)}")
    for v in data.get("variables", []):
        if v["source"] not in data.get("sources", {}):
            raise ValueError(f"variable {v['id']} uses unknown source {v['source']}")
    return data


def load_dictionary(path: Path = DICTIONARY_PATH) -> dict[str, Any]:
    return _load_dictionary_cached(str(path), path.stat().st_mtime)


def variables(devices: Optional[Iterable[str]] = None,
              ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    wanted_dev = {d.lower() for d in devices} if devices else None
    wanted_ids = set(ids) if ids else None
    out = []
    for v in load_dictionary().get("variables", []):
        if wanted_dev is not None and v["device"] not in wanted_dev:
            continue
        if wanted_ids is not None and v["id"] not in wanted_ids:
            continue
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# seasons
# ---------------------------------------------------------------------------
def _norm_season(name: str) -> str:
    return " ".join(name.split())


def _dates_in_names(folder: Path) -> list[date]:
    found: list[date] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return found
    for entry in entries:
        names = [entry.name]
        if entry.is_dir():
            try:
                names = [p.name for p in entry.iterdir()]
            except OSError:
                names = []
        for n in names:
            for m in _DATE_IN_NAME.finditer(n):
                try:
                    found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                except ValueError:
                    pass
    return found


def _data_anchor_date(output_season: Path) -> Optional[date]:
    """Median date across the day-level output CSVs (reads only the date column).

    The median, not the earliest date: a device whose clock was never set can
    log a stray day months away (seen in real data), which would misorder seasons.
    """
    candidates = []
    for pattern in ("*/*_60s_daily_compliance.csv", "*/*_luminosity_metrics.csv",
                    "*/*_expiwell_sleep_metrics.csv"):
        candidates.extend(output_season.glob(pattern))
    dates: list[pd.Timestamp] = []
    for f in candidates:
        try:
            col = pd.to_datetime(pd.read_csv(f, usecols=["date"])["date"], errors="coerce").dropna()
            dates.extend(col.tolist())
        except Exception:
            continue
    if not dates:
        return None
    return pd.Series(dates).sort_values().iloc[len(dates) // 2].date()


def _median_date(values: list[date]) -> date:
    return sorted(values)[len(values) // 2]


def season_numbers(config: Config, participant: str) -> dict[str, dict[str, Any]]:
    """Season folder -> {n, anchor, basis, season_key, season_label, ...}.

    ``n`` is the participant's own 1st, 2nd... season (their progression through
    the year). ``season_key`` is the calendar season the data falls in, which is
    what lines different participants up with each other.

    Looks at both staging and master so an unprocessed earlier season still
    counts. Metadata only: never opens (or hydrates) a staging file.
    """
    seasons: dict[str, dict[str, Any]] = {}
    for root in (config.paths.source_root / participant, config.paths.output_root / participant):
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            key = _norm_season(d.name)
            info = seasons.setdefault(key, {"folders": [], "start": None, "basis": None})
            info["folders"].append(d)

    for key, info in seasons.items():
        start, basis = None, None
        for folder in info["folders"]:
            if folder.is_relative_to(config.paths.output_root):
                anchor = _data_anchor_date(folder)
                if anchor:
                    start, basis = anchor, "data"
        if start is None:
            dated = [dt for folder in info["folders"] for dt in _dates_in_names(folder)]
            if dated:
                start, basis = _median_date(dated), "filenames"
        if start is None:
            m = _SEASON_NAME.match(key)
            if m:
                start, basis = date(int(m.group(2)), _SEASON_MONTH[m.group(1).lower()], 1), "folder name"
        info["start"], info["basis"] = start, basis

    ordered = sorted(seasons.items(), key=lambda kv: (kv[1]["start"] is None, kv[1]["start"] or date.max, kv[0]))
    out: dict[str, dict[str, Any]] = {}
    for i, (name, info) in enumerate(ordered):
        anchor = info["start"]
        entry: dict[str, Any] = {
            "n": i + 1,
            "anchor": anchor.isoformat() if anchor else None,
            "basis": info["basis"],
            "folders": [f.name for f in info["folders"]],
        }
        if anchor:
            kind, year = calendar_season(anchor)
            entry.update({"season_key": season_key(kind, year), "season_label": season_label(kind, year),
                          "season_type": kind, "season_year": year,
                          "season_code": season_code(kind, year)})
        else:
            entry.update({"season_key": None, "season_label": None, "season_type": None,
                          "season_year": None, "season_code": None})
        out[name] = entry
    return out


def _output_season_dir(config: Config, participant: str, season: str) -> Optional[Path]:
    root = config.paths.output_root / participant
    if not root.is_dir():
        return None
    for d in root.iterdir():
        if d.is_dir() and _norm_season(d.name) == _norm_season(season):
            return d
    return None


# ---------------------------------------------------------------------------
# reading one participant-season
# ---------------------------------------------------------------------------
def _read_source(dev_dir: Path, spec: dict[str, Any]) -> list[tuple[str, pd.DataFrame]]:
    """(recording stem, frame) for every file matching the source glob."""
    out = []
    suffix = spec["glob"].lstrip("*")
    for f in sorted(dev_dir.glob(spec["glob"])):
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        stem = f.name[: -len(suffix)] if f.name.endswith(suffix) else f.stem
        out.append((stem, df))
    return out


def _to_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date


def _night_of(var: dict[str, Any], row_date: Optional[date], row: pd.Series) -> Optional[date]:
    if row_date is None:
        return None
    timing = var.get("timing", "day")
    if timing == "noon_to_noon":
        return row_date
    if timing == "diary_morning":
        return row_date - timedelta(days=1)
    if timing == "onset_hour":
        onset = row.get(var.get("onset"))
        if onset is None or pd.isna(onset):
            return None
        return row_date if float(onset) >= 12 else row_date - timedelta(days=1)
    return None


def _affect_dates(dev_dir: Path, spec: dict[str, Any], stem: str, df: pd.DataFrame,
                  notes: list[dict[str, Any]]) -> pd.DataFrame:
    """Give affect rows a real date: own column, else the item-series timestamps."""
    df = df.copy()
    if "date" in df.columns and df["date"].notna().any():
        df["_date"] = _to_date(df["date"])
        return df
    fb = spec.get("timestamp_fallback") or {}
    series_files = sorted(dev_dir.glob(fb.get("glob", "")))
    if series_files:
        try:
            ts = pd.read_csv(series_files[0], usecols=fb["keys"] + [fb["column"]])
            ts = ts.dropna(subset=[fb["column"]]).drop_duplicates(subset=fb["keys"])
            df = df.merge(ts, on=fb["keys"], how="left")
            df["_date"] = _to_date(df[fb["column"]])
            notes.append({"flag": "affect-date-from-series", "severity": "info", "recording": stem})
            return df
        except Exception:
            pass
    df["_date"] = None
    notes.append({"flag": "affect-no-date", "severity": "missing", "recording": stem})
    return df


def _day_status(dev_dir: Path, spec: Optional[dict[str, Any]]) -> dict[str, dict[date, dict[str, Any]]]:
    """recording stem -> date -> {valid, epochs, full} from a daily compliance source."""
    status: dict[str, dict[date, dict[str, Any]]] = {}
    if not spec:
        return status
    full = spec.get("full_day_epochs")
    for stem, df in _read_source(dev_dir, spec):
        if "date" not in df.columns:
            continue
        per: dict[date, dict[str, Any]] = {}
        for _, r in df.iterrows():
            d = pd.to_datetime(r["date"], errors="coerce")
            if pd.isna(d):
                continue
            valid = r.get("valid_day")
            if isinstance(valid, str):
                valid = valid.strip().lower() == "true"
            per[d.date()] = {
                "valid": None if valid is None or pd.isna(valid) else bool(valid),
                "epochs": r.get("epochs"),
                "full": full,
            }
        status[stem] = per
    return status


def _is_partial(var: dict[str, Any], d: Optional[date], days: dict[date, dict[str, Any]]) -> bool:
    if d is None or not days:
        return False

    def short(day: date, need: float) -> bool:
        info = days.get(day)
        if info is None or not info.get("full") or info.get("epochs") is None or pd.isna(info["epochs"]):
            return info is None
        return float(info["epochs"]) < need

    info = days.get(d) or {}
    full = info.get("full")
    if not full:
        return False
    if var.get("timing") == "noon_to_noon":
        # Window noon D -> noon D+1 needs the afternoon of D and the morning of D+1.
        return short(d, full / 2) or short(d + timedelta(days=1), full / 2)
    return short(d, full * PARTIAL_DAY_FRACTION)


def _stem_match(status: dict[str, Any], stem: str) -> Optional[str]:
    """Match a recording stem across sources (actigraph stems share a prefix)."""
    if stem in status:
        return stem
    for other in status:
        if stem.startswith(other) or other.startswith(stem):
            return other
    return next(iter(status), None) if len(status) == 1 else None


def load_participant_season(config: Config, participant: str, season: str,
                            devices: Iterable[str], variable_ids: Optional[Iterable[str]] = None,
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows, notes) for one participant-season. Rows follow LONG_COLUMNS."""
    dictionary = load_dictionary()
    sources = dictionary["sources"]
    rows: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    season_dir = _output_season_dir(config, participant, season)
    if season_dir is None:
        notes.append({"flag": "season-not-in-master", "severity": "missing"})
        return rows, notes

    for device in devices:
        device = device.lower()
        dev_dir = season_dir / _device_folder(config, device)
        chosen = variables([device], variable_ids)
        if not dev_dir.is_dir():
            notes.append({"flag": "device-output-missing", "severity": "missing", "device": device})
            continue
        if device == "actigraph" and not any(dev_dir.glob("*_60s.csv")):
            notes.append({"flag": "actigraph-60s-missing", "severity": "missing", "device": device})

        cache: dict[str, list[tuple[str, pd.DataFrame]]] = {}
        status_cache: dict[str, dict[str, dict[date, dict[str, Any]]]] = {}
        for var in chosen:
            spec = sources[var["source"]]
            if var["source"] not in cache:
                frames = _read_source(dev_dir, spec)
                if var["source"] == "expiwell_affect":
                    frames = [(s, _affect_dates(dev_dir, spec, s, df, notes)) for s, df in frames]
                cache[var["source"]] = frames
                if not frames:
                    notes.append({"flag": "source-missing", "severity": "missing",
                                  "device": device, "source": var["source"]})
            valid_src = (var.get("valid_from") or {}).get("source")
            if valid_src and valid_src not in status_cache:
                status_cache[valid_src] = _day_status(dev_dir, sources[valid_src])
            status = status_cache.get(valid_src, {})

            for stem, df in cache[var["source"]]:
                col = var["column"]
                if col not in df.columns:
                    notes.append({"flag": "column-missing", "severity": "missing", "device": device,
                                  "source": var["source"], "column": col, "recording": stem})
                    continue
                base = {"participant": participant, "season": _norm_season(season), "device": device,
                        "level": var["level"], "recording": stem}

                if var["level"] == "season":
                    if spec.get("per"):
                        for _, r in df.iterrows():
                            slug = re.sub(r"[^a-z0-9]+", "_", str(r[spec["per"]]).lower()).strip("_")
                            rows.append({**base, "variable": f"{var['id']}_{slug}",
                                         "value": _num(r[col])})
                    else:
                        for _, r in df.iterrows():
                            rows.append({**base, "variable": var["id"], "value": _num(r[col])})
                    continue

                days = status.get(_stem_match(status, stem) or "", {}) if status else {}
                date_col = "_date" if "_date" in df.columns else spec.get("date")
                if date_col not in df.columns:
                    continue
                frame = df.copy()
                frame["_d"] = _to_date(frame[date_col]) if date_col != "_date" else frame["_date"]
                if var.get("agg") and "day" in frame.columns:
                    grouped = frame.dropna(subset=["_d"]).groupby("_d")
                    agg = grouped[col].agg(var["agg"])
                    dos = grouped["day"].min()
                    iterable = [(d, {col: agg[d], "day": dos[d]}) for d in agg.index]
                else:
                    iterable = [(r["_d"] if not pd.isna(r["_d"]) else None, r) for _, r in frame.iterrows()]
                for d, r in iterable:
                    r = r if isinstance(r, pd.Series) else pd.Series(r)
                    info = days.get(d) if d else None
                    rows.append({
                        **base,
                        "variable": var["id"],
                        "date": d,
                        "night_of": _night_of(var, d, r),
                        "day_of_study": _num(r.get("day")) if device == "expiwell" else None,
                        "value": _num(r.get(col)),
                        "valid": info.get("valid") if info else None,
                        "partial": _is_partial(var, d, days),
                    })
    return rows, notes


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return 1.0 if value.strip().lower() == "true" else 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


# ---------------------------------------------------------------------------
# selection -> long table
# ---------------------------------------------------------------------------
@dataclass
class T2Selection:
    participants: list[str]
    devices: list[str]
    #: season numbers (1 = first season) to include; None = every season
    seasons: Optional[list[int]] = None
    variables: Optional[list[str]] = None


@dataclass
class T2Frame:
    long: pd.DataFrame
    windows: pd.DataFrame
    flags: list[dict[str, Any]] = field(default_factory=list)
    seasons: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


def load(config: Config, selection: T2Selection) -> T2Frame:
    devices = [d.lower() for d in selection.devices]
    ids = pseudo_id.mapping(config, selection.participants)
    all_rows: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    season_map: dict[str, dict[str, dict[str, Any]]] = {}

    for pid in selection.participants:
        numbers = season_numbers(config, pid)
        season_map[pid] = numbers
        wanted = list(selection.seasons or [])
        by_key = [w for w in wanted if isinstance(w, str)]
        by_number = [w for w in wanted if not isinstance(w, str)]

        def picked(info):
            if not wanted:
                return True
            return info.get("season_key") in by_key or info["n"] in by_number

        chosen = [(name, info) for name, info in numbers.items() if picked(info)]
        if by_key:
            present = {info.get("season_key") for info in numbers.values()}
            for key in sorted(set(by_key) - present):
                # A gap, not a fault: this participant has nothing in that season.
                flags.append({"participant": pid, "season_key": key,
                              "flag": "season-absent", "severity": "info"})
        if by_number:
            present_n = {info["n"] for info in numbers.values()}
            for n in sorted(set(by_number) - present_n):
                flags.append({"participant": pid, "season_n": n, "flag": "season-number-absent",
                              "severity": "info"})
        for name, info in chosen:
            rows, notes = load_participant_season(config, pid, name, devices, selection.variables)
            for r in rows:
                r["season_n"] = info["n"]
                r["season_key"] = info.get("season_key")
                r["season_label"] = info.get("season_label")
                r["season_type"] = info.get("season_type")
                r["season_year"] = info.get("season_year")
                r["display_id"] = ids[pid]
            all_rows.extend(rows)
            for n in notes:
                flags.append({"participant": pid, "season": name, "season_n": info["n"],
                              "season_label": info.get("season_label"), **n})
            if info["basis"] != "data":
                # Without processed data the season comes from filenames (a
                # download date can land in the NEXT season) or the folder name.
                flags.append({"participant": pid, "season": name, "season_n": info["n"],
                              "season_label": info.get("season_label"),
                              "flag": "season-estimated", "severity": "info",
                              "basis": info["basis"]})

    long = pd.DataFrame(all_rows)
    for col in LONG_COLUMNS:
        if col not in long.columns:
            long[col] = None
    long = long[LONG_COLUMNS]
    windows, window_flags = common_windows(long, devices)
    flags.extend(window_flags)
    long = _apply_windows(long, windows)
    for f in flags:                       # screens show the displayed ID only
        if "participant" in f:
            f["display_id"] = ids.get(f["participant"], f["participant"])
    return T2Frame(long=long, windows=windows, flags=flags, seasons=season_map)


def _timing(variable_id: str) -> str:
    base = {v["id"]: v.get("timing", "day") for v in variables()}
    if variable_id in base:
        return base[variable_id]
    return next((t for vid, t in base.items() if variable_id.startswith(vid + "_")), "day")


def common_windows(long: pd.DataFrame, devices: list[str]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Per participant-season: each device's full-day span and their overlap."""
    cols = ["participant", "season", "season_n", "season_key", "season_label",
            "device_spans", "start", "end", "days"]
    flags: list[dict[str, Any]] = []
    day = long[(long["level"] == "day") & long["date"].notna() & (long["partial"] != True)]  # noqa: E712
    records = []
    group_cols = ["participant", "season", "season_n", "season_key", "season_label"]
    for (pid, season, n, skey, slabel), grp in day.groupby(group_cols, dropna=False):
        spans = {}
        for dev, g in grp.groupby("device"):
            # Span = fully recorded CALENDAR days. Night-window variables
            # (noon-to-noon) are complete on a half first day, so they only
            # decide the span when a device has no calendar-day variable.
            calendar = g[g["variable"].map(lambda v: _timing(v) in ("day", "diary_morning"))]
            use = calendar if not calendar.empty else g
            spans[dev] = (min(use["date"]), max(use["date"]))
        missing = [d for d in devices if d not in spans]
        start = max(s for s, _ in spans.values()) if spans else None
        end = min(e for _, e in spans.values()) if spans else None
        days = (end - start).days + 1 if start and end and end >= start else 0
        records.append({
            "participant": pid, "season": season, "season_n": n,
            "season_key": skey, "season_label": slabel,
            "device_spans": {d: [s.isoformat(), e.isoformat()] for d, (s, e) in spans.items()},
            "start": start if days else None, "end": end if days else None, "days": days,
        })
        base = {"participant": pid, "season": season, "season_n": n, "season_label": slabel}
        if missing:
            flags.append({**base, "flag": "device-no-days", "severity": "missing", "devices": missing})
        if len(spans) > 1 and days == 0:
            flags.append({**base, "flag": "no-common-window", "severity": "missing"})
        elif len(spans) > 1 and days < SHORT_WINDOW_DAYS:
            flags.append({**base, "flag": "short-common-window", "severity": "info", "days": days})
    return pd.DataFrame(records, columns=cols), flags


def _apply_windows(long: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    if long.empty or windows.empty:
        long["in_window"] = False if not long.empty else long.get("in_window")
        return long
    w = windows.set_index(["participant", "season"])[["start", "end"]]
    in_window, window_day = [], []
    for pid, season, d in zip(long["participant"], long["season"], long["date"]):
        start, end = w.loc[(pid, season)] if (pid, season) in w.index else (None, None)
        if d is None or pd.isna(d) or start is None or pd.isna(start):
            in_window.append(False)
            window_day.append(None)
        else:
            inside = start <= d <= end
            in_window.append(inside)
            window_day.append((d - start).days + 1 if inside else None)
    long = long.copy()
    long["in_window"] = in_window
    long["window_day"] = window_day
    return long


def wide_day(frame: T2Frame, in_window_only: bool = True, exclude_partial: bool = True) -> pd.DataFrame:
    """One row per participant-day, one column per day-level variable."""
    d = frame.long[frame.long["level"] == "day"]
    if exclude_partial:
        d = d[d["partial"] != True]  # noqa: E712
    if in_window_only:
        d = d[d["in_window"] == True]  # noqa: E712
    if d.empty:
        return pd.DataFrame()
    return (d.pivot_table(index=["display_id", "season_n", "season", "date", "window_day"],
                          columns="variable", values="value", aggfunc="mean")
             .reset_index())


# ---------------------------------------------------------------------------
# dictionary coverage
# ---------------------------------------------------------------------------
def unlisted_columns(config: Config, participants: Iterable[str]) -> dict[str, list[str]]:
    """Output columns in dictionary sources that are neither a variable nor meta."""
    dictionary = load_dictionary()
    listed: dict[str, set[str]] = {}
    for v in dictionary["variables"]:
        listed.setdefault(v["source"], set()).add(v["column"])
    unlisted: dict[str, set[str]] = {}
    for pid in participants:
        root = config.paths.output_root / pid
        if not root.is_dir():
            continue
        for season_dir in (p for p in root.iterdir() if p.is_dir()):
            for name, spec in dictionary["sources"].items():
                dev_dir = season_dir / _device_folder(config, spec["device"])
                for f in dev_dir.glob(spec["glob"]):
                    try:
                        header = list(pd.read_csv(f, nrows=0).columns)
                    except Exception:
                        continue
                    known = listed.get(name, set()) | set(spec.get("meta", [])) | {spec.get("date"), spec.get("timestamp")}
                    extra = [c for c in header if c not in known]
                    if extra:
                        unlisted.setdefault(name, set()).update(extra)
    return {k: sorted(v) for k, v in sorted(unlisted.items())}


def season_verdicts(config: Config, participant: str, season: str, devices: Iterable[str]) -> list[dict[str, Any]]:
    """Compliance verdicts per recording from the *_compliance.json files."""
    out = []
    season_dir = _output_season_dir(config, participant, season)
    if season_dir is None:
        return out
    for device in devices:
        dev_dir = season_dir / _device_folder(config, device)
        for f in sorted(dev_dir.glob("*_compliance.json")):
            try:
                comp = json.loads(f.read_text(encoding="utf-8")).get("compliance", {})
            except (OSError, ValueError):
                continue
            summary = comp.get("summary", {})
            out.append({"participant": participant, "season": _norm_season(season), "device": device.lower(),
                        "recording": f.name[: -len("_compliance.json")], "verdict": comp.get("verdict"),
                        "valid_days": summary.get("valid_days"), "total_days": summary.get("total_days"),
                        "pct_compliance_mean": summary.get("pct_compliance_mean")})
    return out
