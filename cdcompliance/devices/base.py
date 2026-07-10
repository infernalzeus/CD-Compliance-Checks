"""Device processor interface.

Each device (Actigraph now; Expiwell/Saliva/Cognitron/Qualtrics/MiEye later)
implements a `DeviceProcessor`:

- ``discover`` finds the WorkItems inside a season's device folder.
- ``process`` runs that device's checks pipeline for a single WorkItem and
  returns an ``ItemResult``.

Keeping this contract small means adding a device later is a self-contained file
plus a registry entry — and the same contract is exactly what a web UI would
call per selected device.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ItemResult, WorkItem

if TYPE_CHECKING:  # avoid import cycles at runtime
    from ..config import Config, ResolvedSelection
    from ..events import EventBus


class DeviceProcessor(ABC):
    #: lowercase device key, e.g. "actigraph"
    name: str = ""
    #: sub-folder name inside a season folder, e.g. "Actigraph"
    folder_name: str = ""

    @abstractmethod
    def discover(
        self,
        config: "Config",
        participant: str,
        season: str,
        season_dir: Path,
    ) -> list[WorkItem]:
        ...

    @abstractmethod
    def process(
        self,
        item: WorkItem,
        config: "Config",
        selection: "ResolvedSelection",
        bus: "EventBus",
    ) -> ItemResult:
        ...

    def is_complete(self, item: WorkItem, config: "Config") -> bool:
        """True if this item's expected output already exists.

        Used by the master output structure (`manifest`) to colour the grid and
        by the pipeline for selective/idempotent processing. Default: never
        complete (always process). Items with no input are treated as complete
        (nothing to do).
        """
        return item.input_path is None


class NotImplementedDevice(DeviceProcessor):
    """Placeholder for devices whose checks are not built yet."""

    def __init__(self, name: str, folder_name: str) -> None:
        self.name = name
        self.folder_name = folder_name

    def discover(self, config, participant, season, season_dir):  # type: ignore[override]
        return []

    def process(self, item, config, selection, bus):  # type: ignore[override]
        raise NotImplementedError(
            f"Device '{self.name}' compliance checks are not implemented yet."
        )

    def is_complete(self, item, config):  # type: ignore[override]
        return False
