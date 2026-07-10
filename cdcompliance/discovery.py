"""Discover the participant -> season -> device structure on disk.

Season folders are named inconsistently in the source repository (e.g.
``"Autumn  2025"`` has a double space) so we never hard-code them: seasons are
whatever immediate sub-directories exist under the participant folder. Optional
season filtering matches on a whitespace/case-normalised key, so a user can pass
``"Autumn 2025"`` and still match ``"Autumn  2025"``.

Finding the input file(s) inside a device folder is delegated to the device
processor, keeping this module device-agnostic.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import Config, ResolvedSelection
from .events import EventBus
from .devices import get_processor
from .models import WorkItem


def _normalise(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def participant_root(config: Config, participant: str) -> Path:
    return config.paths.source_root / participant


def list_seasons(
    config: Config, participant: str, wanted: Optional[list[str]]
) -> list[Path]:
    root = participant_root(config, participant)
    if not root.exists():
        raise FileNotFoundError(f"Participant folder not found: {root}")

    seasons = sorted(p for p in root.iterdir() if p.is_dir())
    if wanted:
        wanted_keys = {_normalise(w) for w in wanted}
        seasons = [s for s in seasons if _normalise(s.name) in wanted_keys]
    return seasons


def discover(
    config: Config, selection: ResolvedSelection, bus: EventBus
) -> list[WorkItem]:
    """Build the full list of WorkItems for the resolved selection."""
    participant = selection.participant
    seasons = list_seasons(config, participant, selection.seasons)

    items: list[WorkItem] = []
    for season_dir in seasons:
        for device in selection.devices:
            processor = get_processor(device)
            found = processor.discover(
                config=config,
                participant=participant,
                season=season_dir.name,
                season_dir=season_dir,
            )
            items.extend(found)

    bus.emit(
        "discovery",
        participant=participant,
        seasons=[s.name for s in seasons],
        items=[it.label + (f"  <{it.input_path.name}>" if it.input_path else "  (no input)")
               for it in items],
    )
    return items
