"""Who has what, when - the evidence behind "these devices line up".

Panel 2 asks a different question from Panel 1. Panel 1 asks *is it processed*;
Panel 2 asks *does it line up*: are the Actigraph days, the MiEYE days and the
survey response actually from the same stretch of time, for the same visit?

Everything here is derived from dates already written into master - the daily
compliance CSVs for the wearables, the recorded date for a Qualtrics response -
so it needs no new processing and writes nothing.

The reason it exists: the season ordinal in a Qualtrics filename ("S3") has to
mean the participant's third season folder before any cross-device dataset is
believable. Across the whole study that held 19 times out of 19 on 2026-10-30 -
but one re-scheduled visit breaks it, so it is shown per participant rather than
assumed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import results, t2_data
from .config import Config
from .devices import implemented_devices

#: Device output files that carry a per-day date column.
_DAILY_SUFFIXES = ("_60s_daily_compliance.csv", "_daily_compliance.csv")
_DATE_COLUMNS = ("date", "Date", "day", "Day")


def _parse(text: str) -> Optional[date]:
    text = (text or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _daily_dates(output_dir: Path, stem: str) -> list[date]:
    """Every day a recording covers, read from its daily compliance CSV."""
    for suffix in _DAILY_SUFFIXES:
        path = output_dir / f"{stem}{suffix}"
        if not path.exists():
            continue
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            return []
        if not rows:
            return []
        column = next((c for c in _DATE_COLUMNS if c in rows[0]), None)
        if column is None:
            return []
        got = [d for d in (_parse(r.get(column, "")) for r in rows) if d]
        return sorted(set(got))
    return []


@dataclass
class Span:
    """A contiguous-ish stretch of days one recording covers."""

    first: Optional[date]
    last: Optional[date]
    days: int

    def as_dict(self) -> dict[str, Any]:
        return {"first": self.first.isoformat() if self.first else None,
                "last": self.last.isoformat() if self.last else None,
                "days": self.days}


def _span(dates: Iterable[date]) -> Span:
    got = sorted(set(dates))
    return Span(got[0] if got else None, got[-1] if got else None, len(got))


def _overlap_days(a: set[date], b: set[date]) -> int:
    return len(a & b)


def participant_coverage(config: Config, participant: str,
                         devices: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Per season: which devices have data, over which days, and how far they agree.

    `agreement` per season is the share of the union of days that every wearable
    present covers. 1.0 means the devices cover exactly the same days; a low
    number means they were worn at different times and a day-level join across
    them will be mostly empty.
    """
    wanted = {d.lower() for d in (devices or implemented_devices())}
    numbers = t2_data.season_numbers(config, participant)
    meta = {t2_data._norm_season(n): info for n, info in numbers.items()}

    by_season: dict[str, dict[str, Any]] = {}
    day_sets: dict[tuple[str, str], set[date]] = {}
    for item in results.output_items(config, participant):
        device = str(item["device"]).lower()
        if device not in wanted:
            continue
        season = item["season"]
        entry = by_season.setdefault(season, {"season": season, "devices": {}})
        dates = set(_daily_dates(Path(item["output_dir"]), item["stem"]))
        key = (season, device)
        day_sets[key] = day_sets.get(key, set()) | dates
        dev = entry["devices"].setdefault(device, {
            "device": device, "recordings": 0, "verdicts": [], "days": 0,
        })
        dev["recordings"] += 1
        if item.get("verdict"):
            dev["verdicts"].append(item["verdict"])

    seasons: list[dict[str, Any]] = []
    for season, entry in by_season.items():
        info = meta.get(t2_data._norm_season(season), {})
        devs = []
        union: set[date] = set()
        present: list[tuple[str, set[date]]] = []
        for device, dev in sorted(entry["devices"].items()):
            dates = day_sets.get((season, device), set())
            union |= dates
            if dates:
                present.append((device, dates))
            devs.append({**dev, **_span(dates).as_dict()})
        if union and present:
            shared = set.intersection(*[d for _, d in present]) if len(present) > 1 else present[0][1]
            agreement = round(len(shared) / len(union), 3)
        else:
            shared, agreement = set(), None
        pairs = []
        for i, (da, sa) in enumerate(present):
            for db, sb in present[i + 1:]:
                pairs.append({"a": da, "b": db, "shared_days": _overlap_days(sa, sb),
                              "a_days": len(sa), "b_days": len(sb)})
        seasons.append({
            "season": season,
            "label": info.get("season_label") or season,
            "n": info.get("n"),
            "season_key": info.get("season_key"),
            "basis": info.get("basis"),
            "devices": devs,
            "union_days": len(union),
            "shared_days": len(shared),
            "agreement": agreement,
            "pairs": pairs,
            **_span(union).as_dict(),
        })
    seasons.sort(key=lambda s: (s["n"] is None, s["n"] or 0, s["label"]))
    return {"participant": participant, "seasons": seasons,
            "devices": sorted(wanted), "expected_seasons": 4}


def survey_alignment(config: Config, participant: str) -> list[dict[str, Any]]:
    """Where each Qualtrics response falls against the participant's seasons.

    Imported lazily: the archive scan walks the staging tree, and Panel 2 must
    stay usable when staging is offline.
    """
    try:
        from . import qualtrics_intake as qi
    except ImportError:                                   # pragma: no cover
        return []
    try:
        found = qi.scan(config)
    except OSError:                                       # pragma: no cover
        return []
    responses = qi.collect(found, participants=[participant])
    chosen = qi.one_per_slot(responses).values()
    out = [vars(a) | {"recorded": a.recorded.isoformat() if a.recorded else None}
           for a in qi.season_alignment(config, participant, chosen)]
    return out


def selection_coverage(config: Config, participants: Iterable[str],
                       devices: Optional[Iterable[str]] = None,
                       with_surveys: bool = False) -> dict[str, Any]:
    """Coverage for a whole selection, plus the seasons they have in common.

    Participants are lined up on the **season key** (`2025-winter`), never the
    folder name: two participants' second season can be called 'Winter 2025' and
    'Winter  2026' and still be the same calendar season.
    """
    rows = []
    for pid in participants:
        row = participant_coverage(config, pid, devices)
        if with_surveys:
            row["surveys"] = survey_alignment(config, pid)
        rows.append(row)

    shared: dict[str, dict[str, Any]] = {}
    for row in rows:
        for season in row["seasons"]:
            key = season.get("season_key")
            if not key:
                continue
            kind, year = t2_data.parse_season_key(key)
            entry = shared.setdefault(key, {
                "season_key": key, "label": t2_data.season_label(kind, year),
                "participants": [], "devices": {},
            })
            entry["participants"].append(row["participant"])
            for dev in season["devices"]:
                d = entry["devices"].setdefault(dev["device"], {"device": dev["device"],
                                                                "participants": 0, "days": 0})
                d["participants"] += 1
                d["days"] += dev.get("days") or 0
    for entry in shared.values():
        entry["devices"] = sorted(entry["devices"].values(), key=lambda d: d["device"])
    return {
        "participants": rows,
        "shared_seasons": sorted(shared.values(), key=lambda e: e["season_key"]),
        "n_participants": len(rows),
    }
