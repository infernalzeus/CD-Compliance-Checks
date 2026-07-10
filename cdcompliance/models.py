"""Shared data structures passed between pipeline stages.

Kept dependency-free (stdlib only) so any layer — CLI, core, or a future web
UI/API — can import and serialise them without pulling in pandas or the tool
subprocesses.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


@dataclass
class WorkItem:
    """One unit of work: a single device recording within one season."""

    participant: str
    season: str
    device: str
    source_dir: Path
    # Primary input file for the device (e.g. the GENEActiv .bin). May be None
    # when a device folder exists but contains no processable input.
    input_path: Optional[Path] = None
    output_dir: Optional[Path] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.participant} / {self.season} / {self.device}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ComplianceResult:
    """Outcome of a device-specific compliance evaluation."""

    device: str
    verdict: str  # "PASS" | "REVIEW" | "FAIL" | "ERROR"
    summary: dict[str, Any] = field(default_factory=dict)
    per_day: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ItemResult:
    """Result of processing a single WorkItem end to end."""

    item: WorkItem
    status: str  # "done" | "skipped" | "failed"
    output_dir: Optional[Path] = None
    compliance: Optional[ComplianceResult] = None
    artifacts: list[Path] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "item": self.item.to_dict(),
            "status": self.status,
            "output_dir": self.output_dir,
            "compliance": self.compliance.to_dict() if self.compliance else None,
            "artifacts": list(self.artifacts),
            "timings": self.timings,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        return data


@dataclass
class RunResult:
    """Aggregate result for an entire pipeline run."""

    participant: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    items: list[ItemResult] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {"done": 0, "skipped": 0, "failed": 0}
        for item in self.items:
            out[item.status] = out.get(item.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "participant": self.participant,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "counts": self.counts,
            "items": [item.to_dict() for item in self.items],
        }
