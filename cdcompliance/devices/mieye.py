"""MiEYE device processor (M3 light-logger luminosity).

Per WorkItem, end to end:

    1. Ensure the light CSV is downloaded from OneDrive (with progress).
    2. Run the luminosity-metrics tool: it reads the one MiEYE sheet and writes
       the report PDF, per-day metrics CSV, daily-compliance CSV and the
       compliance JSON straight into the output MiEYE folder.
    3. Read that compliance JSON back into a ComplianceResult for the run summary
       and the dashboard.

The MiEYE export is an XLSX saved with a ``.csv`` extension; the tool handles
that. Only the single ``*-logged.csv`` in the folder is read — nothing is written
back into the OneDrive source tree.

Output layout:
    <output_root>/<participant>/<season>/MiEYE/
        <csv_stem>_luminosity_report.pdf
        <csv_stem>_luminosity_metrics.csv
        <csv_stem>_daily_compliance.csv
        <csv_stem>_compliance.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import Config, ResolvedSelection
from ..events import EventBus
from ..models import ComplianceResult, ItemResult, WorkItem
from .. import onedrive
from .. import tools
from .base import DeviceProcessor


class MiEyeProcessor(DeviceProcessor):
    name = "mieye"
    folder_name = "MiEYE"

    def _output_dir(self, config: Config, participant: str, season: str) -> Path:
        return config.paths.output_root / participant / season / config.mieye_folder_name

    def discover(self, config, participant, season, season_dir):
        device_dir = Path(season_dir) / config.mieye_folder_name
        output_dir = self._output_dir(config, participant, season)
        if not device_dir.is_dir():
            return []
        sheets = sorted(device_dir.glob(config.mieye_input_glob))
        if not sheets:
            # Folder present but no logged CSV — record a no-input item so the run
            # summary shows it was seen and skipped.
            return [
                WorkItem(
                    participant=participant, season=season, device=self.name,
                    source_dir=device_dir, input_path=None, output_dir=output_dir,
                )
            ]
        # A season folder holds a single light log; take the first if several.
        return [
            WorkItem(
                participant=participant, season=season, device=self.name,
                source_dir=device_dir, input_path=sheets[0], output_dir=output_dir,
            )
        ]

    # ---- master output structure --------------------------------------------
    def expected_outputs(self, item: WorkItem) -> list[Path]:
        if item.input_path is None or item.output_dir is None:
            return []
        out = Path(item.output_dir)
        stem = Path(item.input_path).stem
        return [
            out / f"{stem}{tools.LUMINOSITY_SUFFIXES['compliance_json']}",
            out / f"{stem}{tools.LUMINOSITY_SUFFIXES['daily']}",
            out / f"{stem}{tools.LUMINOSITY_SUFFIXES['metrics']}",
        ]

    def missing_outputs(self, item: WorkItem) -> list[Path]:
        return [p for p in self.expected_outputs(item) if not p.exists()]

    def is_complete(self, item: WorkItem, config: Config) -> bool:
        if item.input_path is None:
            return True
        expected = self.expected_outputs(item)
        return bool(expected) and all(p.exists() for p in expected)

    def process(
        self,
        item: WorkItem,
        config: Config,
        selection: ResolvedSelection,
        bus: EventBus,
    ) -> ItemResult:
        result = ItemResult(item=item, status="failed", started_at=datetime.now())

        if item.input_path is None:
            result.status = "skipped"
            result.error = "no *-logged.csv in MiEYE folder"
            result.finished_at = datetime.now()
            bus.emit("item_skip", label=item.label, reason=result.error)
            return result

        csv_path = Path(item.input_path)
        output_dir = Path(item.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = csv_path.stem

        try:
            # Resume: if the tool's outputs already exist, reuse them.
            if not selection.force and self.is_complete(item, config):
                bus.emit("step_start", name="luminosity:process")
                bus.emit("step_stdout", name="luminosity:process",
                         line="existing luminosity outputs found — skipping reprocess")
                bus.emit("step_done", name="luminosity:process", seconds=0.0)
                produced = {
                    k: output_dir / f"{stem}{suf}"
                    for k, suf in tools.LUMINOSITY_SUFFIXES.items()
                    if (output_dir / f"{stem}{suf}").exists()
                }
            else:
                onedrive.ensure_local(
                    csv_path, bus,
                    poll_interval=config.onedrive.poll_interval_seconds,
                    stall_timeout=config.onedrive.stall_timeout_seconds,
                    total_timeout=config.onedrive.total_timeout_seconds,
                )
                produced = tools.run_luminosity(config, csv_path, output_dir, bus)

            result.artifacts.extend(produced.values())

            # Read the tool's compliance JSON into a ComplianceResult.
            comp_json = produced.get("compliance_json") or (
                output_dir / f"{stem}{tools.LUMINOSITY_SUFFIXES['compliance_json']}"
            )
            data = json.loads(Path(comp_json).read_text(encoding="utf-8"))
            comp_raw = data.get("compliance", {})
            comp = ComplianceResult(
                device="mieye",
                verdict=comp_raw.get("verdict", "ERROR"),
                summary=comp_raw.get("summary", {}),
                per_day=comp_raw.get("per_day", []),
                parameters=comp_raw.get("parameters", {}),
                notes=comp_raw.get("notes", []),
            )
            result.compliance = comp
            bus.emit(
                "compliance_result",
                label=item.label,
                verdict=comp.verdict,
                summary={
                    "valid_days": comp.summary.get("valid_days"),
                    "total_days": comp.summary.get("total_days"),
                    "pct_compliance_mean": comp.summary.get("pct_compliance_mean"),
                },
            )

            result.status = "done"
            result.output_dir = output_dir
            result.finished_at = datetime.now()
            bus.emit("item_done", label=item.label, status=result.status)
            return result

        except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            result.finished_at = datetime.now()
            bus.emit("item_error", label=item.label, error=result.error)
            return result
