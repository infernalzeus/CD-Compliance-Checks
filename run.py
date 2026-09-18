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
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cdcompliance import batches, naming, pipeline, tools  # noqa: E402
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
        "--initials",
        default=None,
        help="Your initials (2-4 letters). Required for runs that write data; "
             "recorded on the batch.",
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

    initials = batches.normalise_initials(args.initials)
    if not args.dry_run and initials is None:
        print("A run that writes data needs --initials (2-4 letters), e.g. --initials YK",
              file=sys.stderr)
        return 2

    # Ctrl+C sets this, so an in-progress OneDrive hydration or copy aborts
    # instead of running to completion while the traceback is already printing.
    cancel_event = threading.Event()
    recorder = None
    sinks: list = []

    try:
        if args.dry_run:
            # Previews write nothing to the batch records; the plan is kept locally.
            bus, sinks = make_default_bus(jsonl_path=None, verbose=not args.quiet, console=True)
            plan = pipeline.plan(config, resolved, bus)
            preview_dir = batches.private_dir(config) / "previews"
            preview_dir.mkdir(parents=True, exist_ok=True)
            out = preview_dir / f"{datetime.now():%Y%m%d-%H%M%S}-plan.json"
            out.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            print(f"\nDry-run plan written to: {out}")
            return 0

        recorder = batches.BatchRecorder(
            config, stage="PRE", mode="force" if args.force else "new",
            initials=initials, participants=[resolved.participant], source="cli",
        )
        bus, sinks = make_default_bus(jsonl_path=None, verbose=not args.quiet, console=True)
        bus.subscribe(recorder)
        result = pipeline.run(config, resolved, bus, cancel_event=cancel_event)
        try:
            flags = naming.summarise(naming.check_participant(config, resolved.participant))
        except Exception:
            flags = None
        recorder.add_result(result, flags)
        record = recorder.finish("done")
        counts = result.counts
        print(f"\nBatch {recorder.id}: {record['items']}")
        print(f"  public record : {batches.public_dir(config) / (recorder.id + '.json')}")
        print(f"  private detail: {batches.private_dir(config) / recorder.id}")
        # Exit non-zero if anything failed, so schedulers/CI can detect it.
        return 1 if counts.get("failed", 0) else 0
    except KeyboardInterrupt:
        cancel_event.set()
        if recorder is not None:
            try:
                recorder.finish("cancelled")
            except Exception:
                pass
        killed = tools.terminate_all()
        print(
            f"\nInterrupted — cancelled the run"
            + (f" and terminated {killed} tool subprocess(es)." if killed else "."),
            file=sys.stderr,
        )
        return 130  # conventional exit code for SIGINT
    finally:
        for sink in sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
