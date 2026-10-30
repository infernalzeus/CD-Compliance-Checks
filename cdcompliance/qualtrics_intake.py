"""Qualtrics intake: a batch-wise archive fanned out per participant per season.

Every other source in this pipeline arrives as **one file per participant per
season**, discovered under ``<staging>/<CDxxx>/<Season>/<Device>/``. Qualtrics
does not: one export covers **every participant** for one survey and one
ordinal season, and the archive is a flat list of dated upload batches. So this
module is a *fan-out* step that runs before discovery, not a DeviceProcessor.

Three properties follow from the data, each checked against the real archive on
2026-10-30 (read-only) and pinned by tests:

**1. Append, never newest-wins.** One participant's responses for one season can
arrive across several upload batches. The upstream tool keeps only the newest
export per season, which is correct for a cumulative snapshot and wrong here.
Assimilation reads every export and merges on ``ResponseId``.

**2. One row per participant per survey per season.** Confirmed: in the latest
seasonal and retrospective exports every participant appears at most once per
season. Duplicates do occur in ``enrolment`` - abandoned part-finished attempts
beside one completed response - so the winner is chosen explicitly:
finished first, then furthest progress, then most recently recorded.

**3. Once-only surveys are genuinely once-only.** ``enrolment``,
``clinical_enrolment`` and ``clinical_screening`` carry no season and do not
repeat per season (CD008 has exactly one enrolment response, recorded once).
They are written at participant level, beside the season folders, rather than
being forced into one.

The ordinal season in a Qualtrics filename ("S3") is mapped to the
participant's own calendar season folder. Checked across every participant with
master data: 19 of 19 responses agreed, both on ordinal and on the calendar
season of the response date. It is still **verified per participant** rather
than assumed - see :func:`season_alignment`.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import t2_data
from .config import Config

#: Where the archive sits under the staging root.
ARCHIVE_SUBDIR = Path("Data Archive") / "Qualtrics"

#: Folder written beside the other devices in master.
DEVICE_DIRNAME = "Qualtrics"

ONCE_SURVEYS = ("enrolment", "clinical_enrolment", "clinical_screening")
SEASONAL_SURVEYS = ("seasonal", "retrospective")
ALL_SURVEYS = ONCE_SURVEYS + SEASONAL_SURVEYS

ID_COLUMN = "CHIPDID"
RESPONSE_ID = "ResponseId"

# <name>_<date>_<HH.MM>, optionally a browser duplicate suffix " (1)".
# Kept deliberately identical to the upstream tool's rule: the archive is the
# same archive, and two different ideas of what a filename means would be worse
# than one imperfect idea shared.
_EXPORT_RE = re.compile(
    r"^(?P<stem>.+?)_"
    r"(?P<date>[A-Za-z]+ \d{1,2}, ?\d{4}|\d{1,2} [A-Za-z]+ \d{4})"
    r"_(?P<time>\d{1,2}\.\d{2})"
    r"(?: \(\d+\))?$"
)
_DATE_FORMATS = ("%B %d, %Y %H.%M", "%B %d,%Y %H.%M", "%d %B %Y %H.%M")


# ---------------------------------------------------------------- the archive


@dataclass
class Export:
    """One recognised CSV in the archive."""

    path: Path
    survey: str
    season: Optional[int]          # ordinal 1..4, None for once-only or untagged
    exported_at: datetime

    @property
    def batch(self) -> str:
        return self.path.parent.name


@dataclass
class ArchiveScan:
    exports: list[Export] = field(default_factory=list)
    unrecognised: list[Path] = field(default_factory=list)
    #: seasonal/retrospective exports with no season in the filename
    untagged: list[Export] = field(default_factory=list)

    def for_survey(self, survey: str) -> list[Export]:
        return [e for e in self.exports if e.survey == survey]


def archive_root(config: Config) -> Path:
    return Path(config.paths.source_root) / ARCHIVE_SUBDIR


def _parse_timestamp(date_text: str, time_text: str) -> Optional[datetime]:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(f"{date_text} {time_text}", fmt)
        except ValueError:
            pass
    return None


def classify(stem: str) -> tuple[Optional[str], Optional[int]]:
    """Survey type and ordinal season from the part of a filename before the date."""
    s = " ".join(stem.split()).lower()
    m = re.search(r"\(s(\d)\)", s) or re.search(r"\bseason (\d)\b", s)
    season = int(m.group(1)) if m else None
    if "retrospective" in s:
        return "retrospective", season
    if "seasonal" in s or re.fullmatch(r"season \d chip-d", s):
        return "seasonal", season
    if "clinical" in s and "enrol" in s:
        return "clinical_enrolment", None
    if "clinical" in s and "screen" in s:
        return "clinical_screening", None
    if "enrol" in s:
        return "enrolment", None
    return None, None


def parse_export(path: Path) -> Optional[Export]:
    m = _EXPORT_RE.match(path.stem)
    if not m:
        return None
    when = _parse_timestamp(" ".join(m["date"].split()), m["time"])
    survey, season = classify(m["stem"])
    if when is None or survey is None:
        return None
    return Export(path, survey, season, when)


def scan(config: Config, root: Optional[Path] = None) -> ArchiveScan:
    """Every recognised export under the archive, plus what could not be read.

    Read-only: filenames only, nothing is opened. On OneDrive that matters -
    opening a file hydrates it.
    """
    base = Path(root) if root else archive_root(config)
    out = ArchiveScan()
    if not base.exists():
        return out
    for path in sorted(base.rglob("*.csv")):
        export = parse_export(path)
        if export is None:
            out.unrecognised.append(path)
            continue
        out.exports.append(export)
        if export.survey in SEASONAL_SURVEYS and export.season is None:
            out.untagged.append(export)
    return out


def warnings(scan_result: ArchiveScan) -> list[dict[str, Any]]:
    """Archive problems, in the shape Panel 1's naming check already uses.

    Severity follows the same rule as the naming check: ``skipped`` means a file
    is not being read at all, ``warning`` means it is read but something about it
    is off, ``info`` is a remark.
    """
    out: list[dict[str, Any]] = []
    for path in scan_result.unrecognised:
        out.append({
            "severity": "skipped",
            "file": path.name,
            "batch": path.parent.name,
            "message": "filename does not match <survey>_<date>_<HH.MM> - not read",
        })
    for e in scan_result.untagged:
        out.append({
            "severity": "warning",
            "file": e.path.name,
            "batch": e.batch,
            "message": ("no season in the filename (pre-dates the per-season split). "
                        "Its responses are merged on ResponseId, so it adds nothing "
                        "the season-tagged exports do not already carry - but it "
                        "cannot be filed to a season on its own."),
        })
    return out


# ------------------------------------------------------------------- reading


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """A Qualtrics export as header + rows, with its two extra header rows gone.

    Qualtrics writes three header rows: variable names (the real header), the
    question text, and a ``{"ImportId": ...}`` line. The last two are data to
    ``csv`` and noise to everyone else.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open(newline="", encoding=encoding) as fh:
                reader = csv.reader(fh)
                rows = list(reader)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"could not decode {path.name}")
    if not rows:
        return [], []
    header = rows[0]
    body = rows[1:]
    drop = 0
    for i, row in enumerate(body[:2]):
        if any(str(cell).startswith('{"ImportId"') for cell in row):
            drop = i + 1
            break
    return header, [dict(zip(header, r)) for r in body[drop:]]


def _as_date(text: str) -> Optional[date]:
    text = (text or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _progress(row: dict[str, str]) -> int:
    try:
        return int(float(row.get("Progress") or 0))
    except (TypeError, ValueError):
        return 0


def _finished(row: dict[str, str]) -> bool:
    return str(row.get("Finished", "")).strip().lower() in {"true", "1", "yes"}


def better(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    """The response to keep when one participant answered a survey twice.

    Finished beats unfinished; then furthest progress; then most recently
    recorded. Seen in the real archive on ``enrolment`` only, where a handful of
    participants abandoned an attempt and completed another.
    """
    if _finished(a) != _finished(b):
        return a if _finished(a) else b
    if _progress(a) != _progress(b):
        return a if _progress(a) > _progress(b) else b
    da, db = _as_date(a.get("RecordedDate", "")), _as_date(b.get("RecordedDate", ""))
    if da and db and da != db:
        return a if da > db else b
    return a


# ------------------------------------------------------- collecting responses


@dataclass
class Response:
    participant: str
    survey: str
    season: Optional[int]
    row: dict[str, str]
    source: Export

    @property
    def response_id(self) -> str:
        return (self.row.get(RESPONSE_ID) or "").strip()

    @property
    def recorded(self) -> Optional[date]:
        return _as_date(self.row.get("RecordedDate", ""))


def collect(scan_result: ArchiveScan,
            surveys: Optional[Iterable[str]] = None,
            participants: Optional[Iterable[str]] = None) -> list[Response]:
    """Every response in the archive, merged across upload batches.

    **This is the append.** Every export is read, not just the newest per season,
    because one participant's responses for one season arrive split across
    batches. Identity is ``ResponseId``; a response seen in several exports is
    kept once, resolved by :func:`better`.

    A seasonal export with no season in its filename contributes its responses
    with ``season=None``; they merge into the tagged ones by ``ResponseId`` and
    are dropped if they never appear tagged (they carry no season of their own,
    so there is nowhere to file them).
    """
    want_surveys = set(surveys) if surveys else set(ALL_SURVEYS)
    want_pids = {str(p).strip().upper() for p in participants} if participants else None

    # key -> Response. The key deliberately excludes the season, so an untagged
    # copy of a response merges with its tagged twin instead of standing beside it.
    merged: dict[tuple[str, str, str], Response] = {}
    for export in sorted(scan_result.exports, key=lambda e: e.exported_at):
        if export.survey not in want_surveys:
            continue
        try:
            _, rows = _read_rows(export.path)
        except (OSError, ValueError):
            continue
        for row in rows:
            pid = (row.get(ID_COLUMN) or "").strip().upper()
            if not pid:
                continue
            if want_pids is not None and pid not in want_pids:
                continue
            rid = (row.get(RESPONSE_ID) or "").strip()
            if not rid:
                continue
            key = (pid, export.survey, rid)
            found = merged.get(key)
            if found is None:
                merged[key] = Response(pid, export.survey, export.season, row, export)
                continue
            # Keep the better row, and keep any season we have learnt for it.
            season = found.season if found.season is not None else export.season
            winner = better(found.row, row)
            merged[key] = Response(pid, export.survey, season,
                                   winner, found.source if winner is found.row else export)

    out = [r for r in merged.values()
           if r.survey in ONCE_SURVEYS or r.season is not None]
    out.sort(key=lambda r: (r.participant, r.survey, r.season or 0, r.response_id))
    return out


def one_per_slot(responses: Iterable[Response]) -> dict[tuple[str, str, Optional[int]], Response]:
    """Collapse to one response per participant / survey / season.

    The archive says this is already true for the seasonal surveys and not quite
    true for enrolment, so the rule is applied rather than trusted.
    """
    best: dict[tuple[str, str, Optional[int]], Response] = {}
    for r in responses:
        key = (r.participant, r.survey, r.season)
        found = best.get(key)
        if found is None or better(r.row, found.row) is r.row:
            best[key] = r
    return best


# ----------------------------------------------------- ordinal -> calendar season


@dataclass
class Alignment:
    participant: str
    survey: str
    ordinal: int
    recorded: Optional[date]
    #: the participant's own season folder for that ordinal, if they have one
    master_season: Optional[str]
    master_key: Optional[str]
    #: the calendar season the response date itself falls in
    date_key: Optional[str]
    agrees: bool
    note: str = ""


def season_alignment(config: Config, participant: str,
                     responses: Iterable[Response]) -> list[Alignment]:
    """Does Qualtrics' "S3" mean the participant's third season?

    Two independent answers are compared: the **ordinal** (their third season
    folder) and the **date** (the calendar season the response was recorded in).
    When both point at the same season the mapping is proven for that response;
    when they disagree it is reported rather than silently resolved.

    Across the whole study on 2026-10-30 this agreed 19 times out of 19, which
    is why ordinal mapping is used - but it is re-checked per participant,
    because one re-scheduled visit is all it takes to break it.
    """
    numbers = t2_data.season_numbers(config, participant)
    by_ordinal = {info.get("n"): (name, info.get("season_key"))
                  for name, info in numbers.items()}
    out: list[Alignment] = []
    for r in responses:
        if r.survey in ONCE_SURVEYS or r.season is None:
            continue
        name, key = by_ordinal.get(r.season, (None, None))
        date_key = None
        if r.recorded is not None:
            kind, year = t2_data.calendar_season(r.recorded)
            date_key = t2_data.season_key(kind, year)
        if name is None:
            note, agrees = "no season folder for this ordinal yet", False
        elif date_key is None:
            note, agrees = "response has no usable date - ordinal used unchecked", False
        elif date_key == key:
            note, agrees = "", True
        else:
            note, agrees = ("the response date falls in a different calendar season "
                            "than this ordinal's folder"), False
        out.append(Alignment(participant, r.survey, r.season, r.recorded,
                             name, key, date_key, agrees, note))
    out.sort(key=lambda a: (a.survey, a.ordinal))
    return out


# -------------------------------------------------------------------- writing


def target_dir(config: Config, participant: str, season_folder: Optional[str]) -> Path:
    """Where a survey's rows are written.

    Seasonal surveys land beside the other devices, in the participant's own
    season folder. Once-only surveys have no season - forcing them into the
    first one would make them look seasonal - so they sit at participant level.
    """
    root = Path(config.paths.output_root) / participant
    if season_folder is None:
        return root / DEVICE_DIRNAME
    resolved = t2_data._output_season_dir(config, participant, season_folder)
    return (resolved or root / season_folder) / DEVICE_DIRNAME


def _read_existing(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.exists():
        return [], {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    header = list(rows[0].keys()) if rows else []
    return header, {(r.get(RESPONSE_ID) or "").strip(): r for r in rows if r.get(RESPONSE_ID)}


def write_rows(path: Path, rows: list[dict[str, str]], dry_run: bool = False) -> dict[str, Any]:
    """Merge rows into a per-participant CSV on ``ResponseId``.

    **Append, not replace.** What is already there is kept unless the incoming
    copy of the same response is better. A re-run with nothing new rewrites
    nothing and reports ``added=0, updated=0``.
    """
    header, existing = _read_existing(path)
    added = updated = 0
    for row in rows:
        rid = (row.get(RESPONSE_ID) or "").strip()
        if not rid:
            continue
        if rid not in existing:
            existing[rid] = row
            added += 1
        elif better(row, existing[rid]) is row and row != existing[rid]:
            existing[rid] = row
            updated += 1
    columns = list(header)
    for row in existing.values():
        for col in row:
            if col not in columns:
                columns.append(col)
    result = {"path": path, "added": added, "updated": updated, "total": len(existing)}
    if dry_run or not (added or updated):
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(),
                     key=lambda r: ((r.get("RecordedDate") or ""), r.get(RESPONSE_ID) or ""))
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow(row)
    return result


def assimilate(config: Config, participants: Optional[Iterable[str]] = None,
               surveys: Optional[Iterable[str]] = None,
               dry_run: bool = False,
               scan_result: Optional[ArchiveScan] = None) -> dict[str, Any]:
    """Fan the archive out into the per-participant master tree.

    Returns what changed, per file, plus every alignment that could not be
    proven. Nothing is written when ``dry_run``.
    """
    found = scan_result or scan(config)
    responses = collect(found, surveys=surveys, participants=participants)
    chosen = one_per_slot(responses)

    by_pid: dict[str, list[Response]] = {}
    for r in chosen.values():
        by_pid.setdefault(r.participant, []).append(r)

    files: list[dict[str, Any]] = []
    unproven: list[Alignment] = []
    for pid, items in sorted(by_pid.items()):
        numbers = t2_data.season_numbers(config, pid)
        by_ordinal = {info.get("n"): name for name, info in numbers.items()}
        for align in season_alignment(config, pid, items):
            if not align.agrees:
                unproven.append(align)
        grouped: dict[tuple[Optional[str], str], list[dict[str, str]]] = {}
        for r in items:
            season_folder = None if r.survey in ONCE_SURVEYS else by_ordinal.get(r.season)
            if r.survey not in ONCE_SURVEYS and season_folder is None:
                continue        # no folder for that ordinal yet - reported above
            grouped.setdefault((season_folder, r.survey), []).append(r.row)
        for (season_folder, survey), rows in sorted(
                grouped.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
            path = target_dir(config, pid, season_folder) / f"{survey}.csv"
            out = write_rows(path, rows, dry_run=dry_run)
            files.append({**out, "participant": pid, "survey": survey,
                          "season": season_folder, "path": str(path)})
    return {
        "dry_run": dry_run,
        "participants": sorted(by_pid),
        "responses": len(chosen),
        "files": files,
        "added": sum(f["added"] for f in files),
        "updated": sum(f["updated"] for f in files),
        "unproven_alignments": [vars(a) for a in unproven],
        "warnings": warnings(found),
    }


# -------------------------------------------------------------- content check


def content_check(config: Config, participant: str,
                  scan_result: Optional[ArchiveScan] = None) -> dict[str, Any]:
    """Is what the archive holds for this participant actually in master?

    **This is the exception to the pipeline's existence-check rule.** Everywhere
    else, "is it processed?" is answered by whether an output file exists with
    the right name. That cannot work here: a Qualtrics file for one season is
    never finished - a later upload batch can add a response to a season whose
    file already exists. So completeness is answered by comparing **contents**:
    the response ids the archive offers against the ids already written.
    """
    found = scan_result or scan(config)
    responses = collect(found, participants=[participant])
    chosen = one_per_slot(responses)
    numbers = t2_data.season_numbers(config, participant)
    by_ordinal = {info.get("n"): name for name, info in numbers.items()}

    slots: list[dict[str, Any]] = []
    for (pid, survey, ordinal), r in sorted(chosen.items(), key=lambda kv: (kv[0][1], kv[0][2] or 0)):
        season_folder = None if survey in ONCE_SURVEYS else by_ordinal.get(ordinal)
        if survey not in ONCE_SURVEYS and season_folder is None:
            slots.append({"survey": survey, "ordinal": ordinal, "season": None,
                          "state": "no_season_folder", "expected": 1, "present": 0})
            continue
        path = target_dir(config, pid, season_folder) / f"{survey}.csv"
        _, existing = _read_existing(path)
        present = 1 if r.response_id in existing else 0
        slots.append({
            "survey": survey, "ordinal": ordinal, "season": season_folder,
            "state": "assimilated" if present else "outstanding",
            "expected": 1, "present": present, "response_id": r.response_id,
            "recorded": r.recorded.isoformat() if r.recorded else None,
        })
    outstanding = [s for s in slots if s["state"] != "assimilated"]
    if not slots:
        state = "none"
    elif not outstanding:
        state = "complete"
    elif len(outstanding) < len(slots):
        state = "partial"
    else:
        state = "pending"
    return {"participant": participant, "state": state, "slots": slots,
            "n_expected": len(slots), "n_present": len(slots) - len(outstanding)}
