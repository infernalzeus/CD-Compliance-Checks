"""Batches: one record per processing run, split into public and private parts.

A *batch* is one press of RUN (or one CLI run) across any number of
participants. ``PRE`` batches preprocess staging -> master (the Compliance
panel) and carry no season: one run can cover any mix of seasons. ``T2``
records (Visualise -> T2 -> export) are seasonal and carry a season tag such as
``s123`` (the participant's 1st, 2nd and 3rd seasons).

Every batch writes two records:

    runs/batches/<batch-id>.json          PUBLIC  - committed to the public repo
    runs/_private/<batch-id>/             PRIVATE - git-ignored, stays local
        batch.json                         full detail: participants, items,
                                           verdicts, output folders
        events.jsonl                       every pipeline event, in order

The public record is deliberately *stripped*: who ran it (initials), when, the
mode, how many participants and items, per-device processed/skipped/failed
counts, the thresholds in force and each tool's git commit. It contains **no
participant IDs, file or folder paths, filenames, device serial numbers,
recording dates or results**. ``assert_public_safe`` enforces this before the
file is written, so a future change can't leak identifying detail by accident.
Participant IDs stay out until pseudonymous IDs (``pseudo_id.py``) are approved
and switched on; the records can then be refreshed with PIDs.

Removing a batch from the list archives it (``_archive/``) with who removed it
and when - records of processing are never deleted.
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config

SCHEMA_VERSION = 1
STAGES = {
    "PRE": "staging -> master (preprocess)",
    "T2": "master -> T2",
}
#: Stages whose batch id carries a season tag.
SEASONAL_STAGES = {"T2"}
MODES = {"new": "new & unfinished", "force": "force reprocess", "selection": "sent to T2"}

_INITIALS_RE = re.compile(r"^[A-Z]{2,4}$")

# Anything matching these must never appear in a public record.
_LEAK_PATTERNS = [
    (re.compile(r"CD\d{2,}", re.I), "participant ID"),
    (re.compile(r"[A-Za-z]:[\\/]"), "Windows path"),
    (re.compile(r"(^|[\s\"'])/(Users|home|Volumes|mnt)/"), "Unix path"),
    (re.compile(r"\.(bin|csv|xlsx|pdf|awd)\b", re.I), "filename"),
    (re.compile(r"\b\d{6}_\d{4}-\d{2}-\d{2}"), "device serial + recording date"),
]


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def normalise_initials(raw: Any) -> Optional[str]:
    """'yk' -> 'YK'. Returns None unless it's 2-4 letters."""
    value = re.sub(r"[^A-Za-z]", "", str(raw or "")).upper()
    return value if _INITIALS_RE.match(value) else None


def season_tag(seasons) -> str:
    """Calendar season codes -> 'aut25-win25'; visit numbers -> 's24'.

    Calendar seasons are what T2 compares on (one person's 2nd season can be
    another's 3rd), so a T2 batch id normally carries the season codes.
    """
    values = list(seasons)
    if not values:
        raise ValueError("no seasons given")
    if all(isinstance(v, str) and not v.isdigit() for v in values):
        codes = sorted({v.lower() for v in values})
        if any(not re.fullmatch(r"[a-z]{3}\d{2}", c) for c in codes):
            raise ValueError("season codes look like 'aut25'")
        return "-".join(codes)
    nums = sorted({int(n) for n in values})
    if any(n < 1 or n > 9 for n in nums):
        raise ValueError("season ordinals must be 1-9")
    return "s" + "".join(str(n) for n in nums)


def make_batch_id(stage: str, initials: str, when: datetime, seasons=None) -> str:
    """PRE-<yyyymmdd>-<hhmmss>-<initials>            e.g. PRE-20260917-143200-YK
    T2-<yyyymmdd>-<hhmmss>-<initials>-s<seasons>    e.g. T2-20260917-143200-YK-s24
    """
    base = f"{stage}-{when:%Y%m%d-%H%M%S}-{initials}"
    if stage in SEASONAL_STAGES:
        if not seasons:
            raise ValueError(f"{stage} batches need the seasons they cover")
        return f"{base}-{season_tag(seasons)}"
    return base


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def public_dir(config: Config) -> Path:
    return Path(config.runs_dir) / "batches"


def private_dir(config: Config) -> Path:
    return Path(config.runs_dir) / "_private"


def assert_public_safe(record: dict[str, Any]) -> None:
    text = json.dumps(record)
    for pattern, what in _LEAK_PATTERNS:
        m = pattern.search(text)
        if m:
            raise ValueError(f"public batch record would expose a {what}: {m.group(0)!r}")


def _tool_versions(config: Config) -> dict[str, Optional[str]]:
    """Git commit of the dashboard and each tool repo (names only, no paths)."""
    from . import updates

    versions: dict[str, Optional[str]] = {}
    for spec in updates.component_specs(config):
        ok, commit = updates._git(["rev-parse", "--short", "HEAD"], Path(spec["path"]), timeout=10)
        versions[spec["key"]] = commit if ok else None
    return versions


def _thresholds(config: Config) -> dict[str, Any]:
    """Thresholds in force. Path-like settings are reduced to a yes/no flag."""
    xpw = dataclasses.asdict(config.expiwell)
    custom_schedule = bool(xpw.pop("schedule_file", ""))
    xpw["custom_schedule"] = custom_schedule
    return {
        "actigraph": dataclasses.asdict(config.compliance),
        "mieye": dataclasses.asdict(config.luminosity),
        "expiwell": xpw,
    }


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
class BatchRecorder:
    """Collects one batch's events and results, then writes both records."""

    def __init__(self, config: Config, *, stage: str, mode: str, initials: str,
                 participants: list[str], source: str = "dashboard",
                 seasons=None) -> None:
        clean = normalise_initials(initials)
        if clean is None:
            raise ValueError("initials must be 2-4 letters")
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        self.config = config
        self.stage, self.mode, self.initials, self.source = stage, mode, clean, source
        self.participants = list(participants)
        self.started_at = datetime.now()
        self.seasons = list(seasons) if seasons else None
        self.id = make_batch_id(stage, clean, self.started_at, self.seasons)
        self.results: list[dict[str, Any]] = []
        self.naming_counts: Counter = Counter()
        # Two batches can legitimately start in the same second (a second export,
        # a fast double-click), so make the id unique rather than refusing.
        base, suffix = self.id, 2
        while ((private_dir(config) / self.id).exists()
               or (public_dir(config) / f"{self.id}.json").exists()):
            self.id = f"{base}-{suffix}"
            suffix += 1
        self._dir = private_dir(config) / self.id
        self._dir.mkdir(parents=True)
        self._events = (self._dir / "events.jsonl").open("a", encoding="utf-8")

    # EventBus sink
    def __call__(self, event: dict[str, Any]) -> None:
        try:
            self._events.write(json.dumps(event, default=str) + "\n")
            self._events.flush()
        except Exception:
            pass

    def add_result(self, run_result, naming_summary: Optional[dict[str, int]] = None) -> None:
        self.results.append(run_result.to_dict())
        if naming_summary:
            self.naming_counts.update(naming_summary)

    def _item_counts(self) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        overall: Counter = Counter()
        per_device: dict[str, Counter] = defaultdict(Counter)
        for res in self.results:
            for item in res.get("items", []):
                status = item.get("status", "unknown")
                device = (item.get("item") or {}).get("device", "unknown")
                overall[status] += 1
                per_device[device][status] += 1
        return dict(overall), {d: dict(c) for d, c in sorted(per_device.items())}

    def finish(self, status: str, public_extra: Optional[dict[str, Any]] = None,
               private_extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Write both records. *public_extra* is leak-checked like the rest."""
        finished = datetime.now()
        overall, per_device = self._item_counts()
        public = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": self.id,
            "stage": self.stage,
            "stage_label": STAGES[self.stage],
            **({"seasons": self.seasons} if self.seasons else {}),
            "mode": self.mode,
            "mode_label": MODES.get(self.mode, self.mode),
            "initials": self.initials,
            "source": self.source,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "seconds": round((finished - self.started_at).total_seconds(), 1),
            "status": status,
            "n_participants": len(self.participants),
            "items": overall,
            "items_by_device": per_device,
            "naming_flags": {k: self.naming_counts.get(k, 0) for k in ("skipped", "warning", "info")},
            "thresholds": _thresholds(self.config),
            "tool_versions": _tool_versions(self.config),
            "private_detail": "kept locally, not published",
        }
        if self.stage == "T2":           # no processing happens at this stage
            for key in ("items", "items_by_device", "naming_flags"):
                public.pop(key, None)
        public.update(public_extra or {})
        assert_public_safe(public)

        private = {**public, "participants": self.participants, "results": self.results,
                   **(private_extra or {})}
        try:
            self._events.close()
        except Exception:
            pass
        (self._dir / "batch.json").write_text(json.dumps(private, indent=2, default=str),
                                              encoding="utf-8")
        pdir = public_dir(self.config)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{self.id}.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
        return public


# ---------------------------------------------------------------------------
# listing / archiving
# ---------------------------------------------------------------------------
def list_batches(config: Config, include_archived: bool = False,
                 stage: Optional[str] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    folders = [(public_dir(config), False)]
    if include_archived:
        folders.append((public_dir(config) / "_archive", True))
    for folder, archived in folders:
        if not folder.is_dir():
            continue
        for f in folder.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if stage and rec.get("stage") != stage:
                continue
            rec["archived_flag"] = archived
            rec["has_private_detail"] = (
                private_dir(config) / ("_archive" if archived else "") / rec.get("batch_id", "")
            ).is_dir()
            out.append(rec)
    out.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return out


def archive_batch(config: Config, batch_id: str, initials: str) -> dict[str, Any]:
    """Hide a batch from the list, keeping both records with who/when."""
    clean = normalise_initials(initials)
    if clean is None:
        raise ValueError("initials must be 2-4 letters")
    if not re.fullmatch(r"[A-Za-z0-9-]+", batch_id or ""):
        raise ValueError("invalid batch id")
    src = public_dir(config) / f"{batch_id}.json"
    if not src.is_file():
        raise FileNotFoundError(batch_id)
    rec = json.loads(src.read_text(encoding="utf-8"))
    rec["archived"] = {"by": clean, "at": datetime.now().isoformat(timespec="seconds")}
    assert_public_safe(rec)
    dest_dir = public_dir(config) / "_archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{batch_id}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    src.unlink()
    priv = private_dir(config) / batch_id
    if priv.is_dir():
        pa = private_dir(config) / "_archive"
        pa.mkdir(parents=True, exist_ok=True)
        shutil.move(str(priv), str(pa / batch_id))
    return rec


def read_private(config: Config, batch_id: str) -> dict[str, Any]:
    """The local full record of a batch (active or archived)."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", batch_id or ""):
        raise ValueError("invalid batch id")
    for folder in (private_dir(config) / batch_id, private_dir(config) / "_archive" / batch_id):
        f = folder / "batch.json"
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
    raise FileNotFoundError(batch_id)
