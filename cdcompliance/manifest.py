"""Master output structure — the source of truth for 'what should exist'.

Given the source staging tree, this module derives the *expected* output for each
participant (per season, per device) and compares it against what has actually
been generated under the dashboard output tree. It drives:

- the dashboard grid colours (pending / complete / partial), and
- selective processing (the pipeline skips items that are already complete).

Cell state model
----------------
For a participant, across all its (season, device) items that have a processable
input:

    pending  (grey)   : nothing has been processed yet
    complete (white)  : every processable item has its expected output
    partial  (orange) : some items done, some not (e.g. summer done, spring not,
                        or a new season/file appeared in source)
    empty    (grey)   : the participant has no processable input at all

`OUTPUT_STRUCTURE_VERSION` is bumped when the definition of "complete" changes
(e.g. when more devices or new required artifacts are added). This is the piece
we revisit as device coverage grows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .config import ResolvedSelection
from .discovery import discover, participant_root
from .events import EventBus
from .devices import get_processor, implemented_devices
from .models import WorkItem

# Bumped to 2 when MiEYE became an implemented device: the grid + completeness
# now span every implemented device, not just actigraph.
OUTPUT_STRUCTURE_VERSION = 2

_CD_RE = re.compile(r"^CD(?P<suffix>.+)$", re.IGNORECASE)


def cd_suffix(folder_name: str) -> Optional[str]:
    """Return the part after 'CD' (e.g. 'CD011' -> '011'), or None if not a CD folder."""
    m = _CD_RE.match(folder_name.strip())
    return m.group("suffix") if m else None


def list_cd_participants(root: Path) -> list[str]:
    """CD* participant folder names directly under *root* (sorted by suffix)."""
    if not root.exists():
        return []
    names = [p.name for p in root.iterdir() if p.is_dir() and cd_suffix(p.name)]

    def _key(name: str):
        suf = cd_suffix(name) or ""
        digits = re.sub(r"\D", "", suf)
        return (int(digits) if digits else 1 << 30, name)

    return sorted(names, key=_key)


@dataclass
class SeasonDeviceStatus:
    season: str
    device: str
    has_input: bool
    complete: bool
    input_name: Optional[str] = None


@dataclass
class ParticipantStatus:
    participant: str
    suffix: str
    state: str  # pending | complete | partial | empty
    in_output: bool
    done: int
    total: int  # processable items
    items: list[SeasonDeviceStatus] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant": self.participant,
            "suffix": self.suffix,
            "state": self.state,
            "in_output": self.in_output,
            "done": self.done,
            "total": self.total,
            "items": [vars(i) for i in self.items],
        }


def _discover_items(config: Config, participant: str, devices: list[str]) -> list[WorkItem]:
    selection = ResolvedSelection(
        participant=participant,
        seasons=None,
        devices=[d.lower() for d in devices],
        copy_bin=config.copy_bin,
        dry_run=True,
        force=False,
    )
    # A sink-less bus so discovery emits nothing during status queries.
    return discover(config, selection, EventBus())


def participant_status(
    config: Config, participant: str, devices: Optional[list[str]] = None
) -> ParticipantStatus:
    # The grid represents processing state across EVERY implemented device (not
    # just config.devices, which is only the CLI's default run scope). This is
    # what makes a participant with MiEYE data — but no actigraph — show as
    # "not processed" rather than "no data", and a participant with only one of
    # its devices done show as "partial".
    devices = devices or implemented_devices()
    suffix = cd_suffix(participant) or participant
    items = _discover_items(config, participant, devices)

    processable = [it for it in items if it.input_path is not None]
    statuses: list[SeasonDeviceStatus] = []
    done = 0
    for it in items:
        processor = get_processor(it.device)
        complete = processor.is_complete(it, config)
        if it.input_path is not None and complete:
            done += 1
        statuses.append(
            SeasonDeviceStatus(
                season=it.season,
                device=it.device,
                has_input=it.input_path is not None,
                complete=bool(complete),
                input_name=(Path(it.input_path).name if it.input_path else None),
            )
        )

    in_output = (config.paths.output_root / participant).exists()
    total = len(processable)
    if total == 0:
        state = "empty"
    elif done == total:
        state = "complete"       # every expected output present
    elif in_output:
        # The output folder exists but is missing expected outputs (deleted, or
        # a new season/file not yet processed) -> partial (orange), needs a run.
        state = "partial"
    else:
        state = "pending"        # never processed, not in the output tree

    return ParticipantStatus(
        participant=participant,
        suffix=suffix,
        state=state,
        in_output=in_output,
        done=done,
        total=total,
        items=statuses,
    )


def source_grid(config: Config, devices: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Grid model for Panel 1 — every CD folder in the source staging tree."""
    root = config.paths.source_root
    return [
        participant_status(config, name, devices).to_dict()
        for name in list_cd_participants(root)
    ]


def output_grid(config: Config, devices: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Grid model for Panel 2 — CD folders that exist in the output tree."""
    root = config.paths.output_root
    out = []
    for name in list_cd_participants(root):
        st = participant_status(config, name, devices).to_dict()
        out.append(st)
    return out


def pending_items(
    config: Config, participants: list[str], devices: Optional[list[str]] = None
) -> list[WorkItem]:
    """Processable items across *participants* that are not yet complete."""
    devices = devices or implemented_devices()
    pend: list[WorkItem] = []
    for p in participants:
        for it in _discover_items(config, p, devices):
            if it.input_path is None:
                continue
            if not get_processor(it.device).is_complete(it, config):
                pend.append(it)
    return pend
