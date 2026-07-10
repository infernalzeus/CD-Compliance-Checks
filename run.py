#!/usr/bin/env python
"""CD-Compliance-Checks — command-line entry point.

Thin wrapper over ``cdcompliance`` core. Builds a Config + RunSelection from the
config file and CLI flags, wires an event bus (console + JSONL), and runs the
pipeline (or a dry-run plan). The core does the work; this file only handles
argument parsing and output wiring, so a future web UI can reuse the core
unchanged.

Examples
--------
    # Preview what would run (no download, no processing, no writes):
    python run.py --dry-run

    # Process every discovered Actigraph recording for CD011:
    python run.py

    # Only specific seasons, and don't replicate the large raw .bin:
    python run.py --seasons "Winter 2026,Spring 2026" --no-copy-bin

    # A different participant / config:
    python run.py --config config.yaml --participant CD012
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cdcompliance import pipeline  # noqa: E402
from cdcompliance.config import RunSelection, load_config  # noqa: E402
from cdcompliance.devices import all_devices, implemented_devices  # noqa: E402
from cdcompliance.events import make_default_bus  # noqa: E402


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="CD-Compliance-Checks",
        description="Run CHiP-D device compliance checks and replicate outputs.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=HERE / "config.yaml",
        help="Path to the YAML config (default: ./config.yaml).",
    )
    p.add_argument("--participant", default=None, help="Participant ID, e.g. CD011.")
    p.add_argument(
        "--seasons",
        default=None,
        help='Comma-separated season names to include (default: all discovered).',
    )
    p.add_argument(
        "--devices",
        default=None,
        help=f"Comma-separated devices (implemented: {', '.join(implemented_devices())}).",
    )
    p.add_argument(
        "--no-copy-bin",
        action="store_true",
        help="Do not replicate the large raw .bin into the output tree.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report the plan (incl. OneDrive state) without doing work.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Reprocess items even if their output already exists (default: skip).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Less console detail (suppress per-line tool stdout).",
    )
    p.add_argument(
        "--list-devices",
        action="store_true",
        help="List known devices and exit.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_devices:
        for d in all_devices():
            tag = "implemented" if d in implemented_devices() else "pending"
            print(f"  {d:12s} [{tag}]")
        return 0

    if not args.config.exists():
        print(
            f"Config not found: {args.config}\n"
            f"Copy config.example.yaml to config.yaml and edit the paths.",
            file=sys.stderr,
        )
        return 2

    config = load_config(args.config)

    selection = RunSelection(
        participant=args.participant,
        seasons=_split_csv(args.seasons),
        devices=_split_csv(args.devices),
        copy_bin=(False if args.no_copy_bin else None),
        dry_run=args.dry_run,
        force=args.force,
    )
    resolved = selection.resolve(config)

    # Per-run log directory: runs/<participant>_<timestamp>/
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = config.runs_dir / f"{resolved.participant}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"

    bus, sinks = make_default_bus(
        jsonl_path=events_path, verbose=not args.quiet, console=True
    )

    try:
        if args.dry_run:
            plan = pipeline.plan(config, resolved, bus)
            (run_dir / "plan.json").write_text(
                json.dumps(plan, indent=2), encoding="utf-8"
            )
            print(f"\nDry-run plan written to: {run_dir / 'plan.json'}")
            return 0

        result = pipeline.run(config, resolved, bus, run_dir=run_dir)
        counts = result.counts
        print(f"\nRun summary written to: {run_dir / 'run_summary.json'}")
        print(f"Events log: {events_path}")
        # Exit non-zero if anything failed, so schedulers/CI can detect it.
        return 1 if counts.get("failed", 0) else 0
    finally:
        for sink in sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
