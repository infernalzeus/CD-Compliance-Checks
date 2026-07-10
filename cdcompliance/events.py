"""Structured event bus.

Every meaningful pipeline step emits a small, JSON-serialisable event through an
`EventBus`. Sinks subscribe to the bus:

- `ConsoleSink`   — human-readable terminal output, with a live in-place bar for
                    OneDrive downloads and large file copies.
- `JsonlSink`     — appends every event as one JSON line, so a run can be
                    replayed or tailed by an external process / web UI.

A future web UI simply registers its own sink (e.g. pushing each event onto a
websocket) and gets the exact same progress stream the CLI renders — no changes
to the core pipeline required.

Event shape:
    {"type": <str>, "ts": <iso8601>, ...payload}

Known event types (payload keys in parentheses):
    run_start (participant, n_items)
    discovery (participant, seasons, items)
    item_start / item_skip / item_done / item_error (label, ...)
    download_start (file, total_bytes)
    download_progress (file, downloaded_bytes, total_bytes, pct)
    download_done (file, seconds)
    copy_start / copy_progress / copy_done (src, dst, ...)
    step_start / step_done (name)
    step_stdout (name, line)          # streamed stdout from a tool subprocess
    compliance_result (label, verdict, summary)
    log (level, message)
    run_end (counts, seconds)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import _json_default

EventCallback = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._sinks: list[EventCallback] = []

    def subscribe(self, sink: EventCallback) -> None:
        self._sinks.append(sink)

    def emit(self, type: str, **payload: Any) -> dict[str, Any]:
        event = {"type": type, "ts": datetime.now().isoformat(timespec="seconds")}
        event.update(payload)
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:  # a broken sink must never kill the pipeline
                sys.stderr.write(f"[events] sink error: {exc}\n")
        return event

    # Convenience shortcut used throughout the codebase.
    def log(self, message: str, level: str = "info") -> None:
        self.emit("log", level=level, message=message)


class JsonlSink:
    """Append every event as a single JSON line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def __call__(self, event: dict[str, Any]) -> None:
        self._fh.write(json.dumps(event, default=_json_default) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


class ConsoleSink:
    """Human-readable renderer with in-place progress bars."""

    def __init__(self, verbose: bool = True, stream: Any = None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self._bar_active = False

    def _w(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def _end_bar(self) -> None:
        if self._bar_active:
            self._w("\n")
            self._bar_active = False

    @staticmethod
    def _fmt_bytes(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _bar(self, label: str, pct: float, extra: str = "") -> None:
        width = 28
        pct = max(0.0, min(100.0, pct))
        filled = int(round(width * pct / 100.0))
        bar = "#" * filled + "-" * (width - filled)
        self._w(f"\r    {label} [{bar}] {pct:5.1f}%  {extra}   ")
        self._bar_active = True

    def __call__(self, event: dict[str, Any]) -> None:
        etype = event["type"]

        if etype == "run_start":
            self._end_bar()
            self._w(
                f"\n=== Run start: {event.get('participant')} "
                f"({event.get('n_items', '?')} item(s)) ===\n"
            )
        elif etype == "discovery":
            self._end_bar()
            seasons = ", ".join(event.get("seasons", [])) or "(none)"
            self._w(f"Discovered seasons: {seasons}\n")
            for it in event.get("items", []):
                self._w(f"  - {it}\n")
        elif etype == "item_start":
            self._end_bar()
            self._w(f"\n--- {event.get('label')} ---\n")
        elif etype == "item_skip":
            self._end_bar()
            self._w(f"    SKIP: {event.get('reason', '')}\n")
        elif etype == "download_start":
            self._end_bar()
            total = self._fmt_bytes(event.get("total_bytes", 0))
            self._w(f"    OneDrive: hydrating {Path(event['file']).name} ({total})\n")
        elif etype == "download_progress":
            done = self._fmt_bytes(event.get("downloaded_bytes", 0))
            total = self._fmt_bytes(event.get("total_bytes", 0))
            self._bar("download", event.get("pct", 0.0), f"{done} / {total}")
        elif etype == "download_done":
            self._end_bar()
            self._w(f"    OneDrive: local ({event.get('seconds', 0):.1f}s)\n")
        elif etype == "copy_start":
            self._end_bar()
            self._w(f"    Copy: {Path(event['src']).name} -> output\n")
        elif etype == "copy_progress":
            done = self._fmt_bytes(event.get("copied_bytes", 0))
            total = self._fmt_bytes(event.get("total_bytes", 0))
            self._bar("copy    ", event.get("pct", 0.0), f"{done} / {total}")
        elif etype == "copy_done":
            self._end_bar()
        elif etype == "step_start":
            self._end_bar()
            self._w(f"    > {event.get('name')} ...\n")
        elif etype == "step_stdout":
            if self.verbose:
                self._end_bar()
                self._w(f"      | {event.get('line', '')}\n")
        elif etype == "step_done":
            self._end_bar()
            secs = event.get("seconds")
            tail = f" ({secs:.1f}s)" if isinstance(secs, (int, float)) else ""
            self._w(f"    < {event.get('name')} done{tail}\n")
        elif etype == "compliance_result":
            self._end_bar()
            self._w(
                f"    COMPLIANCE: {event.get('verdict')} "
                f"— {event.get('summary', {})}\n"
            )
        elif etype == "item_done":
            self._end_bar()
            self._w(f"    OK: {event.get('label')} [{event.get('status')}]\n")
        elif etype == "item_error":
            self._end_bar()
            self._w(f"    ERROR: {event.get('label')}: {event.get('error')}\n")
        elif etype == "log":
            if self.verbose or event.get("level") in ("warning", "error"):
                self._end_bar()
                self._w(f"    [{event.get('level')}] {event.get('message')}\n")
        elif etype == "run_end":
            self._end_bar()
            self._w(
                f"\n=== Run end: {event.get('counts')} "
                f"in {event.get('seconds', 0):.1f}s ===\n"
            )


def make_default_bus(
    jsonl_path: Path | None = None, verbose: bool = True, console: bool = True
) -> tuple[EventBus, list[Any]]:
    """Build an EventBus wired to console + optional JSONL sinks.

    Returns the bus and the list of sinks (so callers can close the JSONL sink).
    """
    bus = EventBus()
    sinks: list[Any] = []
    if console:
        sink = ConsoleSink(verbose=verbose)
        bus.subscribe(sink)
        sinks.append(sink)
    if jsonl_path is not None:
        jsonl = JsonlSink(jsonl_path)
        bus.subscribe(jsonl)
        sinks.append(jsonl)
    return bus, sinks


class _Timer:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    def seconds(self) -> float:
        return time.perf_counter() - self.t0
