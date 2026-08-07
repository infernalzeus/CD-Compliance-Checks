"""OneDrive Files On-Demand hydration and progress observation (Windows).

The CHiP-D source repository lives in OneDrive with Files On-Demand enabled, so
the large GENEActiv ``.bin`` recordings (~1.1 GB each) are usually *dehydrated*
placeholders: the directory entry reports the full logical size, but almost no
bytes are actually on disk. They must be downloaded before any tool can read
them.

This module:

1. Detects whether a path is a dehydrated placeholder.
2. Triggers hydration (pins the file so OneDrive downloads it).
3. Reports download progress by polling the *physical* (allocated-on-disk) size
   against the logical size, emitting ``download_progress`` events so the CLI —
   or a future web UI — can render a live progress bar.
4. Provides a chunked copy helper that emits ``copy_progress`` events, used when
   replicating the raw ``.bin`` into the dashboard output tree.

All Windows-specific calls degrade gracefully on other platforms / non-OneDrive
paths: a regular local file is simply reported as already available.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .events import EventBus

# File attribute bits (winnt.h). `stat` exposes some of these on Windows only.
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_IS_WINDOWS = sys.platform.startswith("win")

# Treat physical >= 99% of logical as "fully local" — some volumes report
# slightly different allocated sizes than the logical length.
_LOCAL_FRACTION = 0.99


class DownloadCancelled(RuntimeError):
    """Raised when a hydration/copy was aborted via its cancel event.

    A 1 GB hydration can run for many minutes; without this the STOP button and
    server shutdown had nothing to interrupt, so the run kept downloading in the
    background and the process could only be killed by force.
    """

# Load kernel32 with use_last_error so ctypes.get_last_error() is reliable after
# GetCompressedFileSizeW (its INVALID_FILE_SIZE return is ambiguous otherwise).
if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetCompressedFileSizeW = _kernel32.GetCompressedFileSizeW
    _GetCompressedFileSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _GetCompressedFileSizeW.restype = wintypes.DWORD


def _get_physical_size(path: Path) -> int:
    """Bytes actually allocated on disk (0-ish for a dehydrated placeholder)."""
    if not _IS_WINDOWS:
        try:
            return os.stat(path).st_blocks * 512  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return os.path.getsize(path)

    high = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    low = _GetCompressedFileSizeW(str(path), ctypes.byref(high))
    error = ctypes.get_last_error()
    INVALID = 0xFFFFFFFF
    if low == INVALID and error != 0:
        # Fall back to logical size on failure.
        return os.path.getsize(path)
    return (high.value << 32) + low


def _attributes(path: Path) -> int:
    try:
        return os.stat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0


def is_dehydrated(path: Path) -> bool:
    """True if the file appears to be a not-yet-downloaded OneDrive placeholder."""
    path = Path(path)
    attrs = _attributes(path)
    if attrs & (
        FILE_ATTRIBUTE_OFFLINE
        | FILE_ATTRIBUTE_RECALL_ON_OPEN
        | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    ):
        return True
    try:
        logical = os.path.getsize(path)
    except OSError:
        return False
    if logical == 0:
        return False
    return _get_physical_size(path) < logical * _LOCAL_FRACTION


def hydration_status(path: Path) -> dict:
    """Snapshot of a file's local/placeholder state (used by --dry-run)."""
    path = Path(path)
    logical = os.path.getsize(path)
    physical = _get_physical_size(path)
    return {
        "file": str(path),
        "logical_bytes": logical,
        "physical_bytes": physical,
        "pct_local": (physical / logical * 100.0) if logical else 100.0,
        "dehydrated": is_dehydrated(path),
    }


# NOTE: we deliberately do NOT pin files (`attrib +P`). Pinning sets the
# persistent "always keep on this device" state, so OneDrive would keep the
# ~1 GB .bin files hydrated and re-downloading in the background forever — even
# after this app exits. Reading the file (see `_hydrate_reader`) is enough to
# hydrate it just for the run, without leaving that persistent state behind.


def _hydrate_reader(path: Path, state: dict, stop: threading.Event,
                    chunk: int = 4 * 1024 * 1024) -> None:
    """Read the whole file to drive OneDrive hydration, tracking bytes read.

    Reading a placeholder pulls its bytes down through OneDrive; the running
    ``state['read']`` count is the most reliable download-progress signal we can
    observe from Python (the file's on-disk *size* stays near zero until OneDrive
    finishes). Runs in a background thread so the caller can report progress.
    """
    try:
        with open(path, "rb") as fh:
            while not stop.is_set():
                b = fh.read(chunk)
                if not b:
                    break
                state["read"] += len(b)
    except OSError as exc:
        state["err"] = exc
    finally:
        state["done"] = True


def ensure_local(
    path: Path,
    bus: EventBus,
    poll_interval: float = 1.0,
    stall_timeout: float = 30.0,
    total_timeout: float = 3600.0,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Ensure *path* is fully downloaded, emitting live download progress.

    Hydration is driven by reading the file in a background thread; progress is
    the number of bytes read so far (or the on-disk size, whichever is larger),
    so the bar tracks the actual OneDrive download rather than sitting at 0%.

    ``stall_timeout`` is accepted for signature compatibility but no longer used —
    the reader drives hydration from the start, so there is nothing to un-stall.

    Pass ``cancel_event`` (the job's stop flag) to make a download abortable:
    it is checked every poll and stops the reader thread, raising
    ``DownloadCancelled``.
    """
    path = Path(path)
    logical = os.path.getsize(path)

    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled(f"Download of {path.name} cancelled before it started.")

    if not is_dehydrated(path) and _get_physical_size(path) >= logical * _LOCAL_FRACTION:
        bus.emit("download_start", file=str(path), total_bytes=logical)
        bus.emit("download_progress", file=str(path),
                 downloaded_bytes=logical, total_bytes=logical, pct=100.0)
        bus.emit("download_done", file=str(path), seconds=0.0)
        return

    bus.emit("download_start", file=str(path), total_bytes=logical)
    # Hydrate transiently by reading the file (no persistent pin — see note above).
    state = {"read": 0, "done": False, "err": None}
    stop = threading.Event()
    reader = threading.Thread(target=_hydrate_reader, args=(path, state, stop), daemon=True)
    reader.start()

    start = time.monotonic()
    try:
        while not state["done"]:
            downloaded = min(max(state["read"], _get_physical_size(path)), logical)
            pct = (downloaded / logical * 100.0) if logical else 100.0
            bus.emit("download_progress", file=str(path),
                     downloaded_bytes=downloaded, total_bytes=logical, pct=min(pct, 100.0))
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled(
                    f"Download of {path.name} cancelled at {pct:.1f}%."
                )
            if (time.monotonic() - start) > total_timeout:
                raise TimeoutError(
                    f"Timed out after {total_timeout:.0f}s downloading "
                    f"{path.name} ({pct:.1f}%)."
                )
            # Sleep on the cancel event (not time.sleep) so a STOP is acted on
            # immediately instead of after the current poll interval.
            if cancel_event is not None:
                cancel_event.wait(poll_interval)
            else:
                time.sleep(poll_interval)
    finally:
        # Always tell the reader thread to stop; it checks `stop` between chunks
        # so it unwinds within one 4 MB read instead of finishing the whole file.
        stop.set()
        reader.join(timeout=5.0)

    if state["err"] is not None and is_dehydrated(path):
        raise RuntimeError(f"OneDrive hydration failed for {path.name}: {state['err']}")

    bus.emit("download_progress", file=str(path),
             downloaded_bytes=logical, total_bytes=logical, pct=100.0)
    bus.emit("download_done", file=str(path), seconds=time.monotonic() - start)


def copy_with_progress(
    src: Path,
    dst: Path,
    bus: EventBus,
    chunk_size: int = 8 * 1024 * 1024,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Copy *src* to *dst* emitting copy_progress events. Assumes src is local.

    A cancelled copy deletes the half-written destination, so a later run never
    mistakes a truncated ~1 GB replica for a finished one.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = os.path.getsize(src)

    bus.emit("copy_start", src=str(src), dst=str(dst), total_bytes=total)
    copied = 0
    cancelled = False
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            chunk = fsrc.read(chunk_size)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            pct = (copied / total * 100.0) if total else 100.0
            bus.emit(
                "copy_progress",
                src=str(src),
                dst=str(dst),
                copied_bytes=copied,
                total_bytes=total,
                pct=pct,
            )
    if cancelled:
        try:
            dst.unlink()
        except OSError:
            pass
        raise DownloadCancelled(f"Copy of {src.name} cancelled (partial file removed).")
    shutil.copystat(src, dst, follow_symlinks=True)
    bus.emit("copy_done", src=str(src), dst=str(dst))
    return dst
