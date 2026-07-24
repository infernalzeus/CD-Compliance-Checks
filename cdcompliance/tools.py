"""Subprocess wrappers around the two external Actigraph tools.

Both tools rely on bare/relative imports and are invoked as ``python -m cli``
(or ``python -m scripts...``) with ``cwd`` set to their own repo directory. We
stream their stdout/stderr line-by-line into the event bus so progress on a
large ``.bin`` is visible live, and we return the deterministic output paths
(verified to exist) rather than parsing tool chatter.

Step 1 (actigraphy-epoching):
    process : .bin  -> <output_dir>/<bin_stem>_60s.csv  (+ .metadata.json)
    report  : csv   -> <pdf>  (Actigraphy Sleep Report)

Step 2 (actigraphy-sleep-metrics):
    read    : 60s csv -> <repo>/outputs/<stem>_{nonparametric,daily,periodogram,
              sri}.csv + <stem>_report.pdf
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .events import EventBus

_IS_WINDOWS = sys.platform.startswith("win")


class ToolError(RuntimeError):
    pass


class ToolCancelled(RuntimeError):
    """Raised when a running tool subprocess was deliberately terminated."""


# Registry of live tool subprocesses so a shutdown / STOP can terminate them
# (otherwise, on Windows, killing the server orphans the running children).
_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.discard(proc)


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            # /T kills the whole tree (the tool may spawn decode workers).
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def terminate_all() -> int:
    """Kill every live tool subprocess. Returns how many were signalled."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        _kill_proc_tree(proc)
    return len(procs)


def _python_cmd(config: Config) -> list[str]:
    # -u => unbuffered stdout so the tool's verbose lines stream live rather than
    # arriving in one block when the process exits.
    return [config.tools.python_executable, "-u"]


def _stream(cmd: list[str], cwd: Path, bus: EventBus, name: str) -> None:
    """Run a subprocess, streaming merged stdout/stderr to the event bus."""
    bus.emit("step_start", name=name, cmd=cmd, cwd=str(cwd))
    t0 = time.perf_counter()

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        raise ToolError(f"Failed to launch {name}: {exc}") from exc

    _register(proc)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                bus.emit("step_stdout", name=name, line=line)
        proc.wait()
    finally:
        _unregister(proc)

    seconds = time.perf_counter() - t0
    if proc.returncode != 0:
        # A negative code / typical taskkill code means we terminated it.
        raise ToolError(
            f"{name} exited with code {proc.returncode} "
            f"(command: {' '.join(cmd)}, cwd: {cwd})"
        )
    bus.emit("step_done", name=name, seconds=seconds)


def run_step1_process(
    config: Config, bin_path: Path, output_dir: Path, bus: EventBus
) -> Path:
    """Step 1 full pipeline (.bin -> 60-second epoch CSV) with default settings."""
    bin_path = Path(bin_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = _python_cmd(config) + [
        "-m",
        "cli",
        "process",
        "--input",
        str(bin_path),
        "--output-dir",
        str(output_dir),
        "--verbose",
    ]
    _stream(cmd, config.tools.epoching_repo, bus, name="step1:process")

    expected = output_dir / f"{bin_path.stem}_60s.csv"
    if not expected.exists():
        raise ToolError(
            f"Step 1 finished but expected output not found: {expected}"
        )
    return expected


def run_step1_report(
    config: Config, csv_path: Path, output_pdf: Path, bus: EventBus
) -> Path:
    """Step 1 Actigraphy Sleep Report PDF from the 60-second epoch CSV."""
    csv_path = Path(csv_path)
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    cmd = _python_cmd(config) + [
        "-m",
        "scripts.generate_sleep_report",
        "--input",
        str(csv_path),
        "--output",
        str(output_pdf),
        "--verbose",
    ]
    _stream(cmd, config.tools.epoching_repo, bus, name="step1:report")
    if not output_pdf.exists():
        raise ToolError(f"Step 1 report finished but PDF not found: {output_pdf}")
    return output_pdf


# Step 2 output suffixes, keyed by a short logical name.
STEP2_SUFFIXES = {
    "nonparametric": "_nonparametric.csv",
    "daily": "_daily.csv",
    "periodogram": "_periodogram.csv",
    "sri": "_sri.csv",
    "report": "_report.pdf",
}


def run_step2(config: Config, csv_path: Path, bus: EventBus) -> dict[str, Path]:
    """Step 2 metrics. Returns {logical_name: produced_file} for files present.

    Step 2 always writes into ``<sleep_metrics_repo>/outputs/`` named after the
    input CSV stem; callers copy the returned files into the output tree.
    """
    csv_path = Path(csv_path)
    cmd = _python_cmd(config) + [
        "-m",
        "cli",
        "read",
        str(csv_path),
        "--verbose",
    ]
    _stream(cmd, config.tools.sleep_metrics_repo, bus, name="step2:read")

    outputs_dir = config.tools.sleep_metrics_repo / "outputs"
    stem = csv_path.stem
    produced: dict[str, Path] = {}
    for name, suffix in STEP2_SUFFIXES.items():
        candidate = outputs_dir / f"{stem}{suffix}"
        if candidate.exists():
            produced[name] = candidate
    if "report" not in produced:
        bus.log("Step 2 produced no PDF report; continuing.", level="warning")
    return produced


# MiEYE luminosity tool output suffixes, keyed by a short logical name.
LUMINOSITY_SUFFIXES = {
    "compliance_json": "_compliance.json",
    "daily": "_daily_compliance.csv",
    "metrics": "_luminosity_metrics.csv",
    "report": "_luminosity_report.pdf",
}


def run_luminosity(
    config: Config, csv_path: Path, output_dir: Path, bus: EventBus
) -> dict[str, Path]:
    """MiEYE luminosity tool: light CSV -> metrics + compliance + report PDF.

    The luminosity-metrics CLI writes all outputs straight into ``output_dir``
    named after the input stem. Compliance thresholds come from the orchestrator's
    ``luminosity:`` config block and are passed through as CLI flags, so the tool
    owns the computation while the thresholds stay configurable here. Returns
    {logical_name: produced_file} for the files that exist (compliance_json is
    required; the rest are verified best-effort).
    """
    csv_path = Path(csv_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lum = config.luminosity

    cmd = _python_cmd(config) + [
        "cli.py",
        str(csv_path),
        "--output", str(output_dir),
        "--per-day-channel", str(lum.channel),
        "--min-valid-days", str(lum.min_valid_days),
        "--review-margin", str(lum.review_margin_days),
        "--light-floor", str(lum.light_floor_lx),
        "--min-light-hours", str(lum.min_light_hours),
        "--expected-light-hours", str(lum.expected_light_hours),
        "--min-day-night-ratio", str(lum.min_day_night_ratio),
        "--day-window", str(lum.day_window[0]), str(lum.day_window[1]),
        "--night-window", str(lum.night_window[0]), str(lum.night_window[1]),
        "--tat-threshold", str(lum.tat_threshold_lx),
        "--tbt-threshold", str(lum.tbt_threshold_lx),
    ]
    _stream(cmd, config.tools.luminosity_repo, bus, name="luminosity:process")

    stem = csv_path.stem
    produced: dict[str, Path] = {}
    for name, suffix in LUMINOSITY_SUFFIXES.items():
        candidate = output_dir / f"{stem}{suffix}"
        if candidate.exists():
            produced[name] = candidate
    if "compliance_json" not in produced:
        raise ToolError(
            f"Luminosity tool finished but compliance JSON not found: "
            f"{output_dir / (stem + LUMINOSITY_SUFFIXES['compliance_json'])}"
        )
    return produced
