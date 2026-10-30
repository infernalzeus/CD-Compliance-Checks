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
from .devices import all_devices, get_processor, implemented_devices
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


def season_board(
    config: Config, participant: str, devices: Optional[list[str]] = None
) -> dict[str, Any]:
    """Per-season, per-device processing status for one participant (Panel 1, right).

    The grid on the left answers "which participants need attention" with one
    colour per person. This answers "what exactly is outstanding for the person
    I have selected", broken down the way the study is actually run: a
    participant starts in any month and completes a year, so they have up to
    four seasons, each carrying several devices.

    Read-only and metadata-only: it never opens or hydrates a staging file.

    Each season carries BOTH names on purpose:
        ``season``  the real folder name, which is what actions must use
        ``label``   the calendar-season label, which is what people read
    They differ — folders are named by hand ("Autumn  2025" has a double
    space) while ``season_numbers`` returns a whitespace-normalised key. Joining
    the normalised one into a path addresses a folder that does not exist, which
    is how a participant silently disappears from a list.
    """
    from . import results as results_mod
    from . import t2_data                      # local: t2_data -> results -> manifest

    devices = devices or implemented_devices()
    status = participant_status(config, participant, devices)

    # verdicts, keyed by (normalised season, device)
    verdicts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in results_mod.output_items(config, participant):
        key = (t2_data._norm_season(item["season"]), item["device"].lower())
        verdicts.setdefault(key, []).append(item)

    numbers = t2_data.season_numbers(config, participant)
    # normalised key -> ordinal/label metadata
    meta = {t2_data._norm_season(name): info for name, info in numbers.items()}

    # group the real season folders discovery found, keeping the real names
    by_season: dict[str, list] = {}
    for it in status.items:
        by_season.setdefault(it.season, []).append(it)
    # A season present in staging/master but with no discoverable item still shows.
    # `numbers` is keyed by the NORMALISED name, so match on that before adding one:
    # adding it raw would stand a phantom "Autumn 2025" beside the real
    # "Autumn  2025" and show the participant's first season twice.
    seen_norm = {t2_data._norm_season(n) for n in by_season}
    for name in numbers:
        if t2_data._norm_season(name) not in seen_norm:
            by_season[name] = []
            seen_norm.add(t2_data._norm_season(name))

    implemented = set(implemented_devices())
    seasons: list[dict[str, Any]] = []
    for name, items in by_season.items():
        info = meta.get(t2_data._norm_season(name), {})
        rows: list[dict[str, Any]] = []
        for dev in sorted(set(devices) | {i.device for i in items}):
            found = [i for i in items if i.device == dev]
            got = [v for v in verdicts.get((t2_data._norm_season(name), dev), [])]
            # `is_complete()` answers True for an item with no input ("nothing to
            # process"), which is correct for the run loop and misleading for a
            # status light. Completeness here is only meaningful WITH an input.
            with_input = [i for i in found if i.has_input]
            done_n = sum(1 for i in with_input if i.complete)
            if not with_input:
                dev_state = "none"
            elif done_n == len(with_input):
                dev_state = "complete"
            elif done_n:
                dev_state = "partial"
            else:
                dev_state = "pending"
            rows.append({
                "device": dev,
                "implemented": dev in implemented,
                "state": dev_state,
                "has_input": bool(with_input),
                "complete": dev_state == "complete",
                "n_inputs": len(with_input),
                "n_complete": done_n,
                "inputs": [i.input_name for i in found if i.input_name],
                "verdicts": [v.get("verdict") for v in got],
                "n_processed": len(got),
            })
        processable = [r for r in rows if r["has_input"]]
        done = [r for r in processable if r["complete"]]
        if not processable:
            state = "empty"
        elif len(done) == len(processable):
            state = "complete"
        elif done:
            state = "partial"
        else:
            state = "pending"
        seasons.append({
            "season": name,                                   # real folder name
            "label": info.get("season_label") or name,        # what people read
            "n": info.get("n"),                               # 1st..4th season
            "season_key": info.get("season_key"),
            "basis": info.get("basis"),
            "state": state,
            "devices": rows,
        })

    seasons.sort(key=lambda s: (s["n"] is None, s["n"] or 0, s["label"]))
    return {
        "participant": participant,
        "suffix": cd_suffix(participant) or participant,
        "expected_seasons": 4,      # a participant starts any month, completes a year
        "devices_implemented": sorted(implemented),
        "devices_pending": sorted(set(all_devices()) - implemented),
        "seasons": seasons,
    }
