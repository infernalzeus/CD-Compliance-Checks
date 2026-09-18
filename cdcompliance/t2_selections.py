"""T2 phase P1: Visualise -> T2 hand-off and the T2 preview payloads.

A *selection* is what Visualise sends to T2: participants, season numbers,
devices and variables, plus the user's checks. Sending writes **only a record**
(``runs/batches/T2-<ts>-<initials>-s<seasons>.json`` public and
``runs/_private/<id>/batch.json`` private) - never data. The T2 preview then
reads master outputs on demand; nothing is written until Export (phase P5).

Public record (committed): initials, time, season numbers, devices, variable
ids, counts (participant-seasons, paired days, flags by type), common-window
length summary, thresholds and tool versions. No participant IDs, dates or
values - ``batches.assert_public_safe`` enforces it.
Private record (local): the participants, the optional name, the user's checks
and the availability result at the time of sending.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from datetime import date, datetime
from typing import Any, Optional

from . import batches, pseudo_id, t2_data, t2_stats
from .config import Config

ALL_DEVICES = ("actigraph", "mieye", "expiwell")

#: Checks the user confirms on Visualise before sending. ``when`` limits a check
#: to selections that include that device.
USER_CHECKS = {
    "actigraph_60s": {
        "label": "The Actigraph 60 s outputs exist for this selection",
        "when": "actigraph",
    },
    "verdicts_reviewed": {
        "label": "I have reviewed the compliance verdicts for these participants",
        "when": None,
    },
}

FLAG_TEXT = {
    "season-not-in-master": "season is in staging but not processed into master",
    "season-number-absent": "participant has no season with this number",
    "season-absent": "this participant has no data in that season",
    "season-estimated": "season worked out from filenames, not processed data",
    "device-output-missing": "no output folder for this device",
    "actigraph-60s-missing": "Actigraph 60 s output file is missing",
    "source-missing": "an output file used by the selected variables is missing",
    "column-missing": "an output file is missing an expected column",
    "device-no-days": "device has no fully recorded days",
    "no-common-window": "devices do not overlap in time",
    "short-common-window": "devices overlap for less than a week",
    "season-order-estimated": "season number estimated without processed data",
    "affect-date-from-series": "affect dates recovered from the item series",
    "affect-no-date": "affect responses have no dates",
}


class SelectionError(ValueError):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _iso(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_iso(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value != value:        # NaN
        return None
    if hasattr(value, "item"):                              # numpy scalar
        return _iso(value.item())
    return value


def normalise(payload: dict[str, Any]) -> t2_data.T2Selection:
    participants = list(dict.fromkeys(p for p in payload.get("participants", []) if p))
    devices = [d.lower() for d in payload.get("devices", []) if d and d.lower() in ALL_DEVICES]
    devices = devices or list(ALL_DEVICES)
    # Seasons are named by the CALENDAR season the data falls in ("2025-autumn"),
    # because one participant's 2nd season is another's 3rd. Plain numbers are
    # still accepted (older records, and "each participant's Nth season").
    raw_seasons = payload.get("seasons") or []
    seasons: Optional[list] = None
    if raw_seasons:
        keys = sorted({str(x) for x in raw_seasons if not str(x).isdigit()})
        nums = sorted({int(x) for x in raw_seasons if str(x).isdigit()})
        seasons = keys + nums or None
    known = {v["id"] for v in t2_data.variables(devices)}
    wanted = payload.get("variables")
    variables = [v for v in wanted if v in known] if wanted else None
    return t2_data.T2Selection(participants=participants, devices=devices, seasons=seasons,
                               variables=variables or None)


def required_checks(devices: list[str]) -> list[str]:
    return [k for k, c in USER_CHECKS.items() if c["when"] is None or c["when"] in devices]


# ---------------------------------------------------------------------------
# availability summary (shown between Visualise and T2, and atop the preview)
# ---------------------------------------------------------------------------
def summary(config: Config, sel: t2_data.T2Selection, for_handoff: bool = False) -> dict[str, Any]:
    frame = t2_data.load(config, sel)
    long = frame.long
    day = long[long["level"] == "day"] if not long.empty else long

    counts = []
    if not long.empty:
        for (dev, var), g in long.groupby(["device", "variable"]):
            gd = g[g["level"] == "day"]
            counts.append({
                "device": dev, "variable": var, "level": g["level"].iloc[0],
                "participants": int(g["participant"].nunique()), "rows": int(len(g)),
                "in_window": int((gd["in_window"] == True).sum()),  # noqa: E712
                "partial": int((gd["partial"] == True).sum()),      # noqa: E712
            })

    ids = pseudo_id.mapping(config, sel.participants)
    windows = []
    for r in frame.windows.to_dict(orient="records"):
        windows.append({**_iso(r), "display_id": ids.get(r["participant"], r["participant"])})
        windows[-1].pop("participant", None)

    flags = []
    for f in frame.flags:
        if f.get("flag") == "actigraph-60s-missing" and not for_handoff:
            continue           # confirmed before sending; day values come from other files
        f = {k: v for k, v in f.items() if k != "participant"}
        f["text"] = FLAG_TEXT.get(f["flag"], f["flag"])
        flags.append(_iso(f))
    severity = Counter(f.get("severity", "info") for f in flags)

    paired = 0
    if not day.empty:
        inside = day[day["in_window"] == True]  # noqa: E712
        paired = int(inside[["participant", "season", "date"]].drop_duplicates().shape[0])

    season_numbers = sorted({int(w["season_n"]) for w in windows if w.get("season_n") is not None})
    seasons_present: dict[str, dict[str, Any]] = {}
    for w in windows:
        key = w.get("season_key")
        if not key:
            continue
        entry = seasons_present.setdefault(key, {"key": key, "label": w.get("season_label"),
                                                 "n_participant_seasons": 0})
        entry["n_participant_seasons"] += 1
    return {
        "selection": {"n_participants": len(sel.participants), "devices": sel.devices,
                      "seasons": sel.seasons, "variables": sel.variables},
        "privacy": pseudo_id.status(config),
        "windows": windows,
        "flags": flags,
        "flag_counts": {"missing": severity.get("missing", 0), "info": severity.get("info", 0)},
        "counts": counts,
        "paired_days": paired,
        "season_numbers": season_numbers,
        "seasons_present": sorted(seasons_present.values(), key=lambda e: e["key"]),
        "checks": {k: USER_CHECKS[k]["label"] for k in required_checks(sel.devices)},
    }


def _public_extra(sel: t2_data.T2Selection, info: dict[str, Any]) -> dict[str, Any]:
    lengths = [w["days"] for w in info["windows"] if w.get("days")]
    return {
        "devices": sel.devices,
        "variables": sel.variables or [v["id"] for v in t2_data.variables(sel.devices)],
        "n_participant_seasons": len(info["windows"]),
        "paired_days": info["paired_days"],
        "common_window_days": ({"min": min(lengths), "median": statistics.median(lengths),
                                "max": max(lengths)} if lengths else None),
        "availability_flags": dict(Counter(f["flag"] for f in info["flags"])),
    }


# ---------------------------------------------------------------------------
# create / read
# ---------------------------------------------------------------------------
def create(config: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """Record a selection sent from Visualise. Writes records only, never data."""
    initials = batches.normalise_initials(payload.get("initials"))
    if initials is None:
        raise SelectionError("initials required (2-4 letters) to send a selection to T2")
    sel = normalise(payload)
    if not sel.participants:
        raise SelectionError("no participants selected")
    checks = payload.get("checks") or {}
    missing = [k for k in required_checks(sel.devices) if not checks.get(k)]
    if missing:
        raise SelectionError("confirm the checks first: " + "; ".join(USER_CHECKS[k]["label"] for k in missing))

    info = summary(config, sel)
    seasons = sel.seasons or info["season_numbers"]
    if not seasons:
        raise SelectionError("the selection has no processed seasons")
    sel.seasons = seasons
    name = str(payload.get("name") or "").strip()[:80]

    codes = []
    for value in seasons:
        if isinstance(value, str) and not str(value).isdigit():
            kind, year = t2_data.parse_season_key(value)
            codes.append(t2_data.season_code(kind, year))
        else:
            codes.append(int(value))
    recorder = batches.BatchRecorder(config, stage="T2", mode="selection", initials=initials,
                                     participants=sel.participants, source="dashboard",
                                     seasons=codes)
    public = recorder.finish(
        "sent",
        public_extra=_public_extra(sel, info),
        private_extra={
            "name": name,
            "selection": {"participants": sel.participants, "devices": sel.devices,
                          "seasons": seasons, "variables": sel.variables},
            "checks": {k: bool(checks.get(k)) for k in required_checks(sel.devices)},
            "availability_at_send": info,
        },
    )
    return public


def load_selection(config: Config, batch_id: str) -> tuple[dict[str, Any], t2_data.T2Selection]:
    record = batches.read_private(config, batch_id)
    if record.get("stage") != "T2" or "selection" not in record:
        raise SelectionError("not a T2 selection")
    return record, normalise(record["selection"])


def list_selections(config: Config, include_archived: bool = False) -> list[dict[str, Any]]:
    """The selections sent from Visualise, each with the exports made from it.

    Exports are stage-T2 records too, but they are not selections: listing them
    side by side made an export look openable, which it is not.
    """
    records = batches.list_batches(config, include_archived=include_archived, stage="T2")
    exports: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("mode") == "export":
            exports.setdefault(rec.get("export_of", ""), []).append(rec)
    out = []
    for rec in records:
        if rec.get("mode") == "export":
            continue
        try:
            rec["name"] = batches.read_private(config, rec["batch_id"]).get("name", "")
        except (FileNotFoundError, ValueError):
            rec["name"] = ""
        mine = sorted(exports.get(rec["batch_id"], []), key=lambda r: r.get("started_at", ""))
        rec["exports"] = [{"export_id": e.get("export_id"), "at": e.get("started_at"),
                           "by": e.get("initials"), "days": e.get("n_days")} for e in mine]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# timeline preview (one participant-season)
# ---------------------------------------------------------------------------
def timeline(config: Config, sel: t2_data.T2Selection, display_id: str, season_n: int) -> dict[str, Any]:
    ids = pseudo_id.mapping(config, sel.participants)
    reverse = {v: k for k, v in ids.items()}
    participant = reverse.get(display_id)
    if participant is None:
        raise SelectionError("participant is not in this selection")
    one = t2_data.T2Selection(participants=[participant], devices=sel.devices,
                              seasons=[int(season_n)], variables=sel.variables)
    frame = t2_data.load(config, one)
    long = frame.long
    meta = {v["id"]: v for v in t2_data.variables(sel.devices)}
    window = frame.windows.to_dict(orient="records")
    window = _iso(window[0]) if window else None
    if window:
        window.pop("participant", None)

    series, season_values = [], []
    if not long.empty:
        for var, g in long.groupby("variable", sort=False):
            base_id = var if var in meta else next((m for m in meta if var.startswith(m + "_")), var)
            info = meta.get(base_id, {})
            if g["level"].iloc[0] == "season":
                for _, r in g.iterrows():
                    season_values.append({"variable": var, "device": r["device"], "unit": info.get("unit"),
                                          "definition": info.get("definition"), "value": _iso(r["value"])})
                continue
            g = g.sort_values("date")
            series.append({
                "variable": var, "device": g["device"].iloc[0], "unit": info.get("unit"),
                "definition": info.get("definition"), "timing": info.get("timing", "day"),
                "points": [[_iso(r["date"]), _iso(r["value"]), None,
                            bool(r["partial"]), _iso(r["window_day"])]
                           for _, r in g.iterrows()],
            })
    order = {d: i for i, d in enumerate(ALL_DEVICES)}
    ids_order = list(meta)
    series.sort(key=lambda s: (order.get(s["device"], 9), ids_order.index(s["variable"]) if s["variable"] in ids_order else 999))
    flags = [{**{k: v for k, v in f.items() if k != "participant"},
              "text": FLAG_TEXT.get(f["flag"], f["flag"])} for f in frame.flags]
    return {"display_id": display_id, "season_n": int(season_n), "window": window,
            "series": series, "season_values": season_values, "flags": _iso(flags)}


# ---------------------------------------------------------------------------
# cross-device views (phase P2)
# ---------------------------------------------------------------------------
#: Reading every output file again for each click is wasteful, so a selection's
#: long table is kept briefly. Outputs only change when a run writes them, so a
#: short life is safe; ``refresh`` reloads on demand.
CACHE_SECONDS = 120
_FRAME_CACHE: dict[str, tuple[float, Any]] = {}
#: Guard-rail: a matrix of 12 variables is already 66 correlations.
MAX_MATRIX_VARIABLES = 12
#: Scatter points sent to the browser (a big selection would otherwise be huge).
MAX_POINTS = 4000


def frame_for(config: Config, batch_id: str, sel: t2_data.T2Selection, refresh: bool = False):
    hit = _FRAME_CACHE.get(batch_id)
    if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    frame = t2_data.load(config, sel)
    _FRAME_CACHE[batch_id] = (time.time(), frame)
    return frame


def forget(batch_id: str) -> None:
    _FRAME_CACHE.pop(batch_id, None)


def _filters(payload: dict[str, Any]) -> dict[str, Any]:
    weeks, days = payload.get("weeks"), payload.get("days")
    return {
        "weeks": (int(weeks[0]), int(weeks[1])) if weeks and not days else None,
        "days": (int(days[0]), int(days[1])) if days else None,
        "cutoffs": payload.get("cutoffs") or None,
        "in_window_only": bool(payload.get("in_window_only", True)),
        "valid_only": bool(payload.get("valid_only", False)),
        "exclude_partial": bool(payload.get("exclude_partial", True)),
    }


def _meta(variable: str) -> dict[str, Any]:
    for v in t2_data.variables():
        if v["id"] == variable or variable.startswith(v["id"] + "_"):
            return {"id": variable, "device": v["device"], "unit": v.get("unit"),
                    "definition": v.get("definition"), "timing": v.get("timing", "day")}
    return {"id": variable}


def scatter(config: Config, batch_id: str, sel: t2_data.T2Selection,
            payload: dict[str, Any]) -> dict[str, Any]:
    """One variable against another under a lag, with both correlations."""
    x, y = str(payload.get("x", "")), str(payload.get("y", ""))
    lag = str(payload.get("lag", "same"))
    if lag not in t2_stats.LAGS:
        raise SelectionError(f"unknown lag {lag!r}")
    if not x or not y or x == y:
        raise SelectionError("choose two different variables")
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    filters = _filters(payload)
    pairs = t2_stats.pair(frame.long, x, y, lag, **filters)
    stats = t2_stats.correlate(pairs)
    points = [
        {"display_id": r["display_id"], "season_n": int(r["season_n"]) if r["season_n"] == r["season_n"] else None,
         "date": r["date"].isoformat(), "x": _iso(r["x"]), "y": _iso(r["y"])}
        for _, r in pairs.head(MAX_POINTS).iterrows()
    ]
    return {"x": _meta(x), "y": _meta(y), "lag": lag, "lag_label": t2_stats.LAGS[lag],
            "filters": filters, "points": points, "truncated": len(pairs) > MAX_POINTS,
            "stats": stats,
            "exploratory": "Days repeat within people, so within-person and between-person "
                           "associations are reported separately. These previews are exploratory."}


def matrix(config: Config, batch_id: str, sel: t2_data.T2Selection,
           payload: dict[str, Any]) -> dict[str, Any]:
    """Every pair of the chosen variables under one lag, FDR-corrected."""
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    wanted = [v for v in (payload.get("variables") or []) if v]
    if not wanted:
        day_vars = [v["id"] for v in t2_data.variables(sel.devices) if v["level"] == "day"]
        present = set(frame.long["variable"]) if not frame.long.empty else set()
        wanted = [v for v in day_vars if v in present][:MAX_MATRIX_VARIABLES]
    if len(wanted) < 2:
        raise SelectionError("choose at least two variables")
    if len(wanted) > MAX_MATRIX_VARIABLES:
        raise SelectionError(f"at most {MAX_MATRIX_VARIABLES} variables in one matrix")
    out = t2_stats.matrix(frame.long, wanted, str(payload.get("lag", "same")),
                          str(payload.get("which", "within")), **_filters(payload))
    out["meta"] = {v: _meta(v) for v in out["variables"]}
    return out


def seasons_view(config: Config, batch_id: str, sel: t2_data.T2Selection,
                 payload: dict[str, Any]) -> dict[str, Any]:
    """Each variable summarised per season number (person means, not day pooling)."""
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    rows = t2_stats.by_season(frame.long, payload.get("variables") or None, **_filters(payload))
    return {"rows": rows, "meta": {r["variable"]: _meta(r["variable"]) for r in rows}}


# ---------------------------------------------------------------------------
# groups and comparisons (phase P3)
# ---------------------------------------------------------------------------
MAX_GROUPS = 8


def _private_path(config: Config, batch_id: str):
    from pathlib import Path as _Path

    for folder in (batches.private_dir(config) / batch_id,
                   batches.private_dir(config) / "_archive" / batch_id):
        f = folder / "batch.json"
        if f.is_file():
            return _Path(f)
    raise FileNotFoundError(batch_id)


def get_groups(config: Config, batch_id: str) -> dict[str, list[str]]:
    record = batches.read_private(config, batch_id)
    return record.get("groups") or {}


def set_groups(config: Config, batch_id: str, groups: dict[str, Any]) -> dict[str, list[str]]:
    """Save named groups of participants on the selection (local record only).

    Groups are made by the user in T2 - no demographics are involved. They live
    in the private record, because naming who is in a group would identify
    participants in the public one.
    """
    record, sel = load_selection(config, batch_id)
    allowed = set(sel.participants)
    ids = pseudo_id.mapping(config, sel.participants)
    reverse = {v: k for k, v in ids.items()}
    clean: dict[str, list[str]] = {}
    for name, members in (groups or {}).items():
        label = str(name).strip()[:40]
        if not label:
            continue
        chosen = []
        for m in members or []:
            real = reverse.get(m, m)
            if real in allowed and real not in chosen:
                chosen.append(real)
        if chosen:
            clean[label] = chosen
    if len(clean) > MAX_GROUPS:
        raise SelectionError(f"at most {MAX_GROUPS} groups")
    overlap = [p for p in allowed if sum(p in members for members in clean.values()) > 1]
    if overlap:
        raise SelectionError("a participant can only be in one group")

    path = _private_path(config, batch_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["groups"] = clean
    record["groups_updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    forget(batch_id)
    return clean


def compare(config: Config, batch_id: str, sel: t2_data.T2Selection,
            payload: dict[str, Any]) -> dict[str, Any]:
    """One variable, split by group / calendar season / participant."""
    variable = str(payload.get("variable", ""))
    if not variable:
        raise SelectionError("choose a variable")
    by = str(payload.get("by", "season"))
    if by not in t2_stats.COMPARE_BY:
        raise SelectionError(f"unknown comparison {by!r}")
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    ids = pseudo_id.mapping(config, sel.participants)
    reverse = {v: k for k, v in ids.items()}
    only = [reverse.get(p, p) for p in (payload.get("participants") or [])] or None
    groups = get_groups(config, batch_id) if by == "group" else None
    out = t2_stats.compare(frame.long, variable, by=by, groups=groups,
                           participants=only, **_filters(payload))
    out["meta"] = _meta(variable)
    out["groups"] = {name: [ids.get(p, p) for p in members] for name, members in (groups or {}).items()}
    out["participants"] = [ids.get(p, p) for p in sel.participants]
    return _iso(out)


def overlap(config: Config, batch_id: str, sel: t2_data.T2Selection,
            payload: dict[str, Any]) -> dict[str, Any]:
    """Two measures in the same unit, day by day, with how closely they agree."""
    x, y = str(payload.get("x", "")), str(payload.get("y", ""))
    if not x or not y or x == y:
        raise SelectionError("choose two different measures")
    mx, my = _meta(x), _meta(y)
    if mx.get("unit") != my.get("unit"):
        raise SelectionError(f"these are in different units ({mx.get('unit')} vs {my.get('unit')}) - "
                             "overlap only makes sense in the same unit")
    lag = str(payload.get("lag", "same"))
    if lag not in t2_stats.LAGS:
        raise SelectionError(f"unknown lag {lag!r}")
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    filters = _filters(payload)
    pairs = t2_stats.pair(frame.long, x, y, lag, **filters)
    circular = mx.get("unit") in t2_stats.CIRCULAR_UNITS
    tolerance = float(payload.get("tolerance") or t2_stats.DEFAULT_TOLERANCE.get(mx.get("unit"), 1.0))
    stats = t2_stats.agreement(pairs, circular=circular, tolerance=tolerance)
    days = [
        {"display_id": r["display_id"], "date": r["date"].isoformat(),
         "x": _iso(r["x"]), "y": _iso(r["y"])}
        for _, r in pairs.head(MAX_POINTS).iterrows()
    ]
    return {"x": mx, "y": my, "lag": lag, "lag_label": t2_stats.LAGS[lag], "unit": mx.get("unit"),
            "circular": circular, "tolerance": tolerance, "stats": stats, "days": days,
            "filters": filters}


def same_unit_pairs(sel: t2_data.T2Selection) -> dict[str, list[str]]:
    """Measures grouped by unit - only same-unit pairs can be overlapped."""
    groups: dict[str, list[str]] = {}
    chosen = set(sel.variables or [v["id"] for v in t2_data.variables(sel.devices)])
    for v in t2_data.variables(sel.devices):
        if v["level"] != "day" or v["id"] not in chosen:
            continue
        groups.setdefault(v.get("unit") or "", []).append(v["id"])
    return {unit: ids for unit, ids in groups.items() if len(ids) > 1}


# ---------------------------------------------------------------------------
# cut-offs (phase P4) and export (phase P5)
# ---------------------------------------------------------------------------
#: Thresholds that live INSIDE the numbers the tools produced. Moving one of
#: these changes the values themselves, so it needs a re-run in the Compliance
#: panel - a slider in T2 cannot do it.
BAKED_IN_THRESHOLDS = [
    {"name": "light floor (melanopic lx)", "affects": "light_hours, light_pct_compliance",
     "where": "luminosity-metrics"},
    {"name": "TAT / TBT levels", "affects": "light_tat_min, light_tbt_min", "where": "luminosity-metrics"},
    {"name": "day / night windows", "affects": "light_day_mean, light_night_mean", "where": "luminosity-metrics"},
    {"name": "non-wear rule (axis SD, run length)", "affects": "act_wear_hours, act_nonwear_hours",
     "where": "the dashboard's compliance step"},
    {"name": "epoch length (60 s)", "affects": "every Actigraph measure", "where": "actigraphy-epoching"},
    {"name": "ESM expected prompts", "affects": "esm_response_rate", "where": "expiwell-metrics"},
]


def cutoff_options(config: Config, batch_id: str, sel: t2_data.T2Selection,
                   payload: dict[str, Any]) -> dict[str, Any]:
    """Slider bounds per measure, plus what the current cut-offs leave behind."""
    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    filters = _filters(payload)
    without = dict(filters, cutoffs=None)
    base = t2_stats.day_frame(frame.long, **without)
    kept = t2_stats.day_frame(frame.long, **filters)

    def counts(d):
        if d.empty:
            return {"days": 0, "participants": 0}
        return {"days": int(d[["participant", "season", "date"]].drop_duplicates().shape[0]),
                "participants": int(d["participant"].nunique())}

    ranges = t2_stats.measure_ranges(frame.long, payload.get("variables"), **without)
    for r in ranges:
        r["meta"] = _meta(r["variable"])
    return {"ranges": ranges, "before": counts(base), "after": counts(kept),
            "cutoffs": filters.get("cutoffs") or [], "operators": t2_stats.OPERATORS,
            "needs_reprocessing": BAKED_IN_THRESHOLDS}


def export_preview(config: Config, batch_id: str, sel: t2_data.T2Selection,
                   payload: dict[str, Any]) -> dict[str, Any]:
    from . import t2_export

    frame = frame_for(config, batch_id, sel, bool(payload.get("refresh")))
    filters = _filters(payload)
    frame = _filtered_frame(frame, filters)
    return t2_export.preview(config, batch_id, sel, frame, {**filters, **payload})


def export_write(config: Config, batch_id: str, sel: t2_data.T2Selection,
                 payload: dict[str, Any]) -> dict[str, Any]:
    from . import t2_export

    record, _ = load_selection(config, batch_id)
    frame = frame_for(config, batch_id, sel, refresh=True)     # always the current outputs
    filters = _filters(payload)
    trimmed = _filtered_frame(frame, filters)
    result = t2_export.write(config, batch_id, sel, trimmed, {**filters, **payload},
                             approver=payload.get("approver", ""), name=record.get("name", ""))
    recorder = batches.BatchRecorder(config, stage="T2", mode="export",
                                     initials=result["approved_by"],
                                     participants=sel.participants, source="dashboard",
                                     seasons=record.get("seasons") or [])
    inclusion = result["inclusion"]
    recorder.finish("exported", public_extra={
        "export_of": batch_id,
        "export_id": result["export_id"],
        "n_participant_seasons": inclusion["participant_seasons"],
        "n_days": inclusion["days"],
        "n_measures": inclusion["measures"],
        "devices": inclusion["devices"],
        "day_range": inclusion["day_range"],
        "part_days": inclusion["part_days"],
        "cut_offs": inclusion["cut_offs"],
        # names only, without extensions: the public guard (rightly) rejects
        # anything that looks like a filename
        "files": [f["name"].rsplit(".", 1)[0] for f in result["files"]],
        "n_files": len(result["files"]),
    }, private_extra={"destination": result["destination"], "manifest": result["manifest"]})
    return result


def _filtered_frame(frame, filters: dict[str, Any]):
    """A copy of the frame whose day rows have the preview's filters applied."""
    import dataclasses

    day = t2_stats.day_frame(frame.long, **filters)
    season_rows = frame.long[frame.long["level"] == "season"]
    keep_keys = set(map(tuple, day[["participant", "season"]].drop_duplicates().to_numpy()))
    season_rows = season_rows[[tuple(k) in keep_keys
                               for k in season_rows[["participant", "season"]].to_numpy()]]
    import pandas as pd

    return dataclasses.replace(frame, long=pd.concat([day, season_rows], ignore_index=True))
