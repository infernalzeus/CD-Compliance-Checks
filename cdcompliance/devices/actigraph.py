"""Actigraph device processor (GENEActiv .bin).

Per WorkItem, end to end:

    1. Ensure the .bin is downloaded from OneDrive (with live progress).
    2. Step 1: .bin -> 60-second epoch CSV (default settings) into the output
       Actigraph folder. This CSV carries SVM_sum.
    3. Step 1 report PDF from that CSV.
    4. Compliance: valid-day wear rule on the 60-second epochs.
    5. Step 2: circadian/sleep metrics from the same CSV; copy its outputs in.
    6. Build the per-item Excel workbook.
    7. Replicate the raw .bin into the output folder (optional).

Output layout:
    <output_root>/<participant>/<season>/Actigraph/
        <bin_stem>.bin                     (raw replica, if copy_bin)
        <bin_stem>_60s.csv (+ .metadata.json)
        <bin_stem>_60s_sleep_report.pdf    (Step 1 report)
        <bin_stem>_60s_report.pdf          (Step 2 report)
        <bin_stem>_60s_{nonparametric,daily,periodogram,sri}.csv
        <bin_stem>_compliance.xlsx
        <bin_stem>_compliance.json
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..config import Config, ResolvedSelection
from ..events import EventBus
from ..models import ComplianceResult, ItemResult, WorkItem
from ..models import _json_default
from .. import compliance as compliance_mod
from .. import excel as excel_mod
from .. import onedrive
from .. import tools
from .base import DeviceProcessor


class ActigraphProcessor(DeviceProcessor):
    name = "actigraph"
    folder_name = "Actigraph"

    def discover(self, config, participant, season, season_dir):
        device_dir = Path(season_dir) / config.actigraph_folder_name
        if not device_dir.is_dir():
            return []
        bins = sorted(device_dir.glob(config.actigraph_bin_glob))
        output_dir = (
            config.paths.output_root
            / participant
            / season
            / config.actigraph_folder_name
        )
        if not bins:
            # A device folder exists but has no .bin — record a no-input item so
            # the run summary shows it was seen and skipped.
            return [
                WorkItem(
                    participant=participant,
                    season=season,
                    device=self.name,
                    source_dir=device_dir,
                    input_path=None,
                    output_dir=output_dir,
                )
            ]
        return [
            WorkItem(
                participant=participant,
                season=season,
                device=self.name,
                source_dir=device_dir,
                input_path=b,
                output_dir=output_dir,
            )
            for b in bins
        ]

    # ---- master output structure --------------------------------------------
    def expected_outputs(self, item: WorkItem) -> list[Path]:
        """The required output files a completed run always produces.

        This is the 'master folder output structure' for Actigraph: if ANY of
        these is missing the item is treated as incomplete (needs re-processing)
        and the participant cell shows partial/orange. Best-effort artifacts that
        can legitimately fail (Step 1/Step 2 PDFs, Step 2 CSVs, the raw .bin
        replica) are intentionally NOT required here.
        """
        if item.input_path is None or item.output_dir is None:
            return []
        out = Path(item.output_dir)
        stem = Path(item.input_path).stem          # <bin stem>
        epoch_stem = f"{stem}_60s"
        # Metadata JSON is a Step-1 byproduct (not used downstream and not
        # regenerable on a Step-1-skipped resume), so it is intentionally not
        # required for completeness.
        return [
            out / f"{epoch_stem}.csv",                  # Step 1 epoch CSV (resume anchor)
            out / f"{stem}_compliance.json",            # compliance verdict
            out / f"{stem}_compliance.xlsx",            # combined workbook
            out / f"{epoch_stem}_daily_compliance.csv", # daily %compliance
        ]

    def missing_outputs(self, item: WorkItem) -> list[Path]:
        return [p for p in self.expected_outputs(item) if not p.exists()]

    def is_complete(self, item: WorkItem, config: Config) -> bool:
        if item.input_path is None:
            return True  # nothing to process for this season/device
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
        timings: dict[str, float] = {}

        if item.input_path is None:
            result.status = "skipped"
            result.error = "no .bin file in Actigraph folder"
            result.finished_at = datetime.now()
            bus.emit("item_skip", label=item.label, reason=result.error)
            return result

        bin_path = Path(item.input_path)
        output_dir = Path(item.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = bin_path.stem  # e.g. "CD011_left wrist_109248_2025-12-02 10-42-59"

        force = selection.force
        epoch_csv = output_dir / f"{stem}_60s.csv"
        bin_hydrated = False

        try:
            # 1 + 2. Step 1 (.bin -> 60s epoch CSV). RESUME: if the epoch CSV
            # already exists, reuse it and skip BOTH the OneDrive download and the
            # epoching — Step 1 is the only stage that needs the raw .bin. This is
            # what makes re-running a partial folder cheap (no 1 GB download / decode).
            if not force and epoch_csv.exists():
                bus.emit("step_start", name="step1:process")
                bus.emit(
                    "step_stdout",
                    name="step1:process",
                    line="existing 60s CSV found — skipping OneDrive download + epoching",
                )
                bus.emit("step_done", name="step1:process", seconds=0.0)
                bus.log("Step 1 output present — reusing existing 60s CSV.")
                timings["step1_process"] = 0.0
            else:
                t = time.perf_counter()
                onedrive.ensure_local(
                    bin_path,
                    bus,
                    poll_interval=config.onedrive.poll_interval_seconds,
                    stall_timeout=config.onedrive.stall_timeout_seconds,
                    total_timeout=config.onedrive.total_timeout_seconds,
                    cancel_event=selection.cancel_event,
                )
                timings["hydrate"] = time.perf_counter() - t
                bin_hydrated = True

                t = time.perf_counter()
                epoch_csv = tools.run_step1_process(config, bin_path, output_dir, bus)
                timings["step1_process"] = time.perf_counter() - t

            result.artifacts.append(epoch_csv)
            meta = epoch_csv.with_suffix(".metadata.json")
            if meta.exists():
                result.artifacts.append(meta)

            # 3. Step 1 report PDF (skip if already present).
            t = time.perf_counter()
            step1_pdf = output_dir / f"{epoch_csv.stem}_sleep_report.pdf"
            if not force and step1_pdf.exists():
                bus.log("Step 1 report present — skipping.")
            else:
                try:
                    tools.run_step1_report(config, epoch_csv, step1_pdf, bus)
                except tools.ToolError as exc:
                    bus.log(f"Step 1 report failed (non-fatal): {exc}", level="warning")
            if step1_pdf.exists():
                result.artifacts.append(step1_pdf)
            timings["step1_report"] = time.perf_counter() - t

            # 4. Compliance on the 60s epochs.
            t = time.perf_counter()
            comp = compliance_mod.evaluate(epoch_csv, config.compliance)
            timings["compliance"] = time.perf_counter() - t
            result.compliance = comp
            # Daily report CSV (with pct_compliance) — read by the dashboard.
            daily_csv = output_dir / f"{epoch_csv.stem}_daily_compliance.csv"
            compliance_mod.write_daily_compliance_csv(comp, daily_csv)
            result.artifacts.append(daily_csv)
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

            # 5. Step 2 metrics (skip if its outputs already exist).
            t = time.perf_counter()
            step2_local: dict[str, Path] = {}
            step2_expected = {
                key: output_dir / f"{epoch_csv.stem}{suffix}"
                for key, suffix in tools.STEP2_SUFFIXES.items()
            }
            if not force and all(p.exists() for p in step2_expected.values()):
                bus.log("Step 2 outputs present — skipping.")
                step2_local = dict(step2_expected)
                result.artifacts.extend(step2_expected.values())
            else:
                try:
                    step2_files = tools.run_step2(config, epoch_csv, bus)
                    for key, src in step2_files.items():
                        dst = output_dir / Path(src).name
                        onedrive.copy_with_progress(
                            src, dst, bus, cancel_event=selection.cancel_event
                        )
                        step2_local[key] = dst
                        result.artifacts.append(dst)
                except tools.ToolError as exc:
                    bus.log(f"Step 2 failed (non-fatal): {exc}", level="warning")
            timings["step2"] = time.perf_counter() - t

            # 6. Per-item Excel workbook.
            t = time.perf_counter()
            context = {
                "participant": item.participant,
                "season": item.season,
                "device": item.device,
                "input_file": bin_path.name,
                "source_dir": str(item.source_dir),
                "output_dir": str(output_dir),
                "epoch_csv": epoch_csv.name,
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
            xlsx = output_dir / f"{stem}_compliance.xlsx"
            excel_mod.build_item_workbook(xlsx, context, comp, step2_local)
            result.artifacts.append(xlsx)

            # Compliance JSON alongside the workbook.
            comp_json = output_dir / f"{stem}_compliance.json"
            comp_json.write_text(
                json.dumps(
                    {"context": context, "compliance": comp.to_dict()},
                    indent=2,
                    default=_json_default,
                ),
                encoding="utf-8",
            )
            result.artifacts.append(comp_json)
            timings["excel"] = time.perf_counter() - t

            # 7. Replicate the raw .bin into the output tree (optional, large).
            if selection.copy_bin:
                t = time.perf_counter()
                dst_bin = output_dir / bin_path.name
                if dst_bin.exists() and dst_bin.stat().st_size == bin_path.stat().st_size:
                    bus.log("Raw .bin already present in output; skipping copy.")
                    result.artifacts.append(dst_bin)
                elif bin_hydrated:
                    onedrive.copy_with_progress(
                        bin_path, dst_bin, bus, cancel_event=selection.cancel_event
                    )
                    result.artifacts.append(dst_bin)
                else:
                    # Step 1 was reused, so the .bin was never downloaded. Don't
                    # pull ~1 GB just to archive it — run with force to re-copy.
                    bus.log(
                        "Raw .bin replica missing but Step 1 was reused — skipping "
                        ".bin copy (use force reprocess to re-copy it).",
                        level="warning",
                    )
                timings["copy_bin"] = time.perf_counter() - t

            result.status = "done"
            result.output_dir = output_dir
            result.timings = timings
            result.finished_at = datetime.now()
            bus.emit("item_done", label=item.label, status=result.status)
            return result

        except (onedrive.DownloadCancelled, tools.ToolCancelled) as exc:
            # User pressed STOP / the server is shutting down. Not an error —
            # report it as cancelled so the run summary and UI don't show a
            # spurious failure.
            result.status = "cancelled"
            result.error = str(exc)
            result.timings = timings
            result.finished_at = datetime.now()
            bus.emit("item_cancelled", label=item.label, reason=str(exc))
            return result

        except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            result.timings = timings
            result.finished_at = datetime.now()
            bus.emit("item_error", label=item.label, error=result.error)
            return result
