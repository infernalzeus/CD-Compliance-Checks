"""Run orchestration.

`run()` is the single entry point the CLI (and, later, a web API) calls. It
discovers WorkItems, delegates each to its device processor, writes a run
summary + participant roll-up, and returns a `RunResult`. All progress flows
through the `EventBus`, so the caller controls how it is surfaced.

`plan()` powers ``--dry-run``: it discovers items and reports what *would*
happen (including each input's OneDrive hydration state and size) without
downloading, processing, or writing anything.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import discovery, onedrive
from .config import Config, ResolvedSelection, RunSelection
from .events import EventBus
from .models import ItemResult, RunResult, _json_default
from .devices import get_processor


def plan(config: Config, selection: ResolvedSelection, bus: EventBus) -> dict[str, Any]:
    items = discovery.discover(config, selection, bus)
    planned: list[dict[str, Any]] = []
    for it in items:
        entry: dict[str, Any] = {
            "label": it.label,
            "input": str(it.input_path) if it.input_path else None,
            "output_dir": str(it.output_dir) if it.output_dir else None,
            "action": "skip (no input)" if it.input_path is None else "process",
        }
        if it.input_path is not None:
            try:
                entry["onedrive"] = onedrive.hydration_status(it.input_path)
            except OSError as exc:
                entry["onedrive"] = {"error": str(exc)}
        planned.append(entry)
        bus.log(
            f"PLAN {entry['action']}: {it.label}"
            + (f"  <{Path(it.input_path).name}>" if it.input_path else "")
        )
        if it.input_path is not None:
            od = entry.get("onedrive", {})
            gb = od.get("logical_bytes", 0) / (1024 ** 3)
            bus.log(
                f"      size={gb:.2f} GB  local={od.get('pct_local', 0):.1f}%  "
                f"needs_download={od.get('dehydrated')}"
            )
    return {
        "participant": selection.participant,
        "copy_bin": selection.copy_bin,
        "devices": selection.devices,
        "items": planned,
    }


def run(
    config: Config,
    selection: RunSelection | ResolvedSelection,
    bus: EventBus,
    run_dir: Path | None = None,
    cancel_event=None,
) -> RunResult:
    resolved = (
        selection.resolve(config)
        if isinstance(selection, RunSelection)
        else selection
    )

    started = datetime.now()
    t0 = time.perf_counter()
    result = RunResult(participant=resolved.participant, started_at=started)

    items = discovery.discover(config, resolved, bus)
    bus.emit("run_start", participant=resolved.participant, n_items=len(items))

    for item in items:
        if cancel_event is not None and cancel_event.is_set():
            bus.emit("run_cancelled", participant=resolved.participant)
            break

        processor = get_processor(item.device)

        # Selective processing: skip items whose expected output already exists
        # (unless --force). This is what stops already-done seasons from being
        # reprocessed every run.
        if not resolved.force and processor.is_complete(item, config):
            reason = (
                "no input" if item.input_path is None else "already processed"
            )
            bus.emit("item_skip", label=item.label, reason=reason)
            result.items.append(
                ItemResult(
                    item=item,
                    status="skipped",
                    output_dir=item.output_dir,
                    error=reason,
                    started_at=datetime.now(),
                    finished_at=datetime.now(),
                )
            )
            continue

        bus.emit("item_start", label=item.label)
        item_result = processor.process(item, config, resolved, bus)
        result.items.append(item_result)

        # Roll each successfully processed item into the participant summary.
        if item_result.status == "done" and item_result.compliance is not None:
            _append_summary(config, item_result)

    result.finished_at = datetime.now()
    bus.emit(
        "run_end",
        counts=result.counts,
        seconds=time.perf_counter() - t0,
    )

    if run_dir is not None:
        _write_run_summary(run_dir, result)
    return result


def _append_summary(config: Config, item_result) -> None:
    from . import excel as excel_mod

    item = item_result.item
    comp = item_result.compliance
    summary_csv = (
        config.paths.output_root / item.participant / f"{item.participant}_compliance_summary.csv"
    )
    # Common columns shared by every device.
    row = {
        "participant": item.participant,
        "season": item.season,
        "device": item.device,
        "input_file": Path(item.input_path).name if item.input_path else "",
        "verdict": comp.verdict,
        "valid_days": comp.summary.get("valid_days"),
        "total_days": comp.summary.get("total_days"),
        "total_epochs": comp.summary.get("total_epochs"),
        "pct_compliance_mean": comp.summary.get("pct_compliance_mean"),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(item_result.output_dir) if item_result.output_dir else "",
    }
    # Device-specific key metrics (kept out of each other's rows so the roll-up
    # stays meaningful per device).
    if item.device == "mieye":
        adequacy = comp.summary.get("light_adequacy") or {}
        row.update(
            channel=comp.parameters.get("channel"),
            day_mean_medi=adequacy.get("day_mean_medi"),
            night_mean_medi=adequacy.get("night_mean_medi"),
            tat_min_per_day_mean=adequacy.get("tat_min_per_day_mean"),
        )
    else:
        row.update(
            svm_mean=comp.summary.get("svm_mean"),
            svm_p99=comp.summary.get("svm_p99"),
            activity_threshold_svm=comp.parameters.get("activity_threshold_svm"),
        )
    try:
        excel_mod.append_participant_summary(summary_csv, row)
    except Exception as exc:  # summary is best-effort
        print(f"[pipeline] participant summary update failed: {exc}")


def _write_run_summary(run_dir: Path, result: RunResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run_summary.json"
    path.write_text(
        json.dumps(result.to_dict(), indent=2, default=_json_default),
        encoding="utf-8",
    )
