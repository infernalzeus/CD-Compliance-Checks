"""Expiwell device processor (ExpiWell experience-sampling surveys).

Unlike the other devices the input is a **folder** of survey CSVs (one per
survey), not a single recording, so the whole ``Expiwell`` folder is the work
item and outputs are named after the participant.

Per WorkItem, end to end:

    1. Ensure every survey CSV in the folder is downloaded from OneDrive.
    2. Run the expiwell-metrics tool: it parses the four-row ExpiWell preamble,
       scores response rates against the configured survey schedule, and writes
       the report PDF, metrics/response CSVs, daily-compliance CSV and the
       compliance JSON straight into the output Expiwell folder.
    3. Read that compliance JSON back into a ComplianceResult for the run summary
       and the dashboard.

Because an ExpiWell export lists only *completed* responses, the compliance
denominator comes from the configured schedule (``expiwell:`` in config.yaml plus
the tool's per-survey protocol), never from the file itself.

Output layout:
    <output_root>/<participant>/<season>/Expiwell/
        <participant>_compliance.json
        <participant>_daily_compliance.csv
        <participant>_expiwell_metrics.csv
        <participant>_expiwell_responses.csv
        <participant>_expiwell_report.pdf
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


class ExpiwellProcessor(DeviceProcessor):
    name = "expiwell"
    folder_name = "Expiwell"

    def _output_dir(self, config: Config, participant: str, season: str) -> Path:
        return config.paths.output_root / participant / season / config.expiwell_folder_name

    def _stem(self, item: WorkItem) -> str:
        """Outputs are named after the participant (the folder holds many CSVs)."""
        return item.participant

    @staticmethod
    def _survey_files(config: Config, device_dir: Path) -> list[Path]:
        """Survey CSVs to process from an Expiwell folder.

        Convention is ``CDxxx-Expiwell-Data-<Survey>.csv``. Exports that were
        never renamed (``Affect.csv``) still sit in the right participant/season
        folder, so the folder identifies them; fall back to every CSV there and
        let expiwell-metrics validate each file's ExpiWell preamble.
        """
        files = sorted(device_dir.glob(config.expiwell_input_glob))
        if not files:
            files = sorted(p for p in device_dir.glob("*.csv") if p.is_file())
        return files

    def discover(self, config, participant, season, season_dir):
        device_dir = Path(season_dir) / config.expiwell_folder_name
        output_dir = self._output_dir(config, participant, season)
        if not device_dir.is_dir():
            return []
        surveys = self._survey_files(config, device_dir)
        # The folder itself is the input; None marks "seen but nothing to do".
        return [
            WorkItem(
                participant=participant, season=season, device=self.name,
                source_dir=device_dir,
                input_path=device_dir if surveys else None,
                output_dir=output_dir,
                extra={"n_surveys": len(surveys)},
            )
        ]

    # ---- master output structure --------------------------------------------
    def expected_outputs(self, item: WorkItem) -> list[Path]:
        if item.input_path is None or item.output_dir is None:
            return []
        out = Path(item.output_dir)
        stem = self._stem(item)
        return [
            out / f"{stem}{tools.EXPIWELL_SUFFIXES['compliance_json']}",
            out / f"{stem}{tools.EXPIWELL_SUFFIXES['daily']}",
            out / f"{stem}{tools.EXPIWELL_SUFFIXES['metrics']}",
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
            result.error = "no Expiwell survey CSVs in the folder"
            result.finished_at = datetime.now()
            bus.emit("item_skip", label=item.label, reason=result.error)
            return result

        folder = Path(item.input_path)
        output_dir = Path(item.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = self._stem(item)

        try:
            if not selection.force and self.is_complete(item, config):
                bus.emit("step_start", name="expiwell:process")
                bus.emit("step_stdout", name="expiwell:process",
                         line="existing Expiwell outputs found - skipping reprocess")
                bus.emit("step_done", name="expiwell:process", seconds=0.0)
                produced = {
                    k: output_dir / f"{stem}{suf}"
                    for k, suf in tools.EXPIWELL_SUFFIXES.items()
                    if (output_dir / f"{stem}{suf}").exists()
                }
            else:
                # The survey CSVs are small but may still be OneDrive placeholders.
                for csv_path in self._survey_files(config, folder):
                    onedrive.ensure_local(
                        csv_path, bus,
                        poll_interval=config.onedrive.poll_interval_seconds,
                        stall_timeout=config.onedrive.stall_timeout_seconds,
                        total_timeout=config.onedrive.total_timeout_seconds,
                        cancel_event=selection.cancel_event,
                    )
                produced = tools.run_expiwell(
                    config, folder, output_dir, stem, item.season, bus
                )

            result.artifacts.extend(produced.values())

            comp_json = produced.get("compliance_json") or (
                output_dir / f"{stem}{tools.EXPIWELL_SUFFIXES['compliance_json']}"
            )
            data = json.loads(Path(comp_json).read_text(encoding="utf-8"))
            # The tool writes the compliance object at the top level.
            comp_raw = data.get("compliance", data)
            comp = ComplianceResult(
                device="expiwell",
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
                    "response_rate_pct": comp.summary.get("overall_response_rate_pct"),
                    "completed": comp.summary.get("total_completed"),
                    "expected": comp.summary.get("total_expected"),
                },
            )

            result.status = "done"
            result.output_dir = output_dir
            result.finished_at = datetime.now()
            bus.emit("item_done", label=item.label, status=result.status)
            return result

        except (onedrive.DownloadCancelled, tools.ToolCancelled) as exc:
            result.status = "cancelled"
            result.error = str(exc)
            result.finished_at = datetime.now()
            bus.emit("item_cancelled", label=item.label, reason=str(exc))
            return result

        except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            result.finished_at = datetime.now()
            bus.emit("item_error", label=item.label, error=result.error)
            return result
