"""Step 2 for Qualtrics: run the survey tool's cleaning over assimilated rows.

The split, and why it is this way round:

    archive  --assimilate-->  <survey>.csv       (raw, accumulates)
                              <survey>_clean.csv (derived, rebuilt each time)

``qualtrics-preprocessing`` holds the study's cleaning rules: drop preview
responses, drop the Qualtrics plumbing columns, keep the named item list for
each survey and rename the items to readable headings. Those rules belong to
the survey team and are theirs to change, so they are **imported from the repo**
rather than copied here. If the repo is not cloned, assimilation still works and
only the raw file is written.

Cleaning **drops ``ResponseId``**, which is the key assimilation merges on. That
is why cleaning cannot run first and why the clean file is rebuilt from the raw
file every time rather than appended to: after cleaning there is nothing left to
match a later upload batch against.

The repo is a flat set of modules (``clean.common``, ``discovery``, ...), not an
installed package, so it is imported by putting its directory on ``sys.path``
for the duration of the import and no longer.
"""
from __future__ import annotations

import contextlib
import csv
import io
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from .config import Config

#: Suffix for the derived file, beside <survey>.csv.
CLEAN_SUFFIX = "_clean.csv"

#: Importing a flat repo by path is not thread-safe; runs are serialised anyway
#: but the grid can ask while a run is going.
_IMPORT_LOCK = threading.Lock()


def repo_path(config: Config) -> Optional[Path]:
    path = getattr(config.tools, "qualtrics_repo", None)
    return Path(path) if path else None


def available(config: Config) -> bool:
    """Is the survey tool cloned and does it look like itself?"""
    path = repo_path(config)
    return bool(path and (path / "clean" / "common.py").is_file())


def _load_clean(config: Config):
    """``clean.common.clean`` from the repo, or None.

    Imported fresh each time the repo path changes so a ``git pull`` on the tool
    is picked up without restarting the dashboard.
    """
    path = repo_path(config)
    if not available(config):
        return None
    root = str(path.resolve())
    with _IMPORT_LOCK:
        added = root not in sys.path
        if added:
            sys.path.insert(0, root)
        try:
            for name in [m for m in sys.modules
                         if m == "clean" or m.startswith("clean.")
                         or m in {"discovery", "load", "surveys"}]:
                mod = sys.modules[name]
                origin = getattr(getattr(mod, "__spec__", None), "origin", "") or ""
                if origin and not origin.startswith(root):
                    # a module of the same name from somewhere else - drop it
                    del sys.modules[name]
            from clean.common import clean  # type: ignore
            return clean
        except Exception:
            return None
        finally:
            if added:
                with contextlib.suppress(ValueError):
                    sys.path.remove(root)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns: list[str] = []
    for row in rows:
        for col in row:
            if col not in columns:
                columns.append(col)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def clean_path(raw_path: Path) -> Path:
    return raw_path.with_name(raw_path.stem + CLEAN_SUFFIX)


def rebuild(config: Config, raw_path: Path, survey: str,
            season: Optional[int] = None) -> dict[str, Any]:
    """Derive ``<survey>_clean.csv`` from ``<survey>.csv``.

    Returns what happened. Never raises: a cleaning rule that does not fit the
    rows it is given is a problem to report, not a reason to fail the run and
    lose the raw data that is already safely written.
    """
    out: dict[str, Any] = {"path": str(clean_path(raw_path)), "state": "skipped",
                           "rows_in": 0, "rows_out": 0, "note": "", "log": ""}
    if not available(config):
        out["note"] = "qualtrics-preprocessing is not cloned - raw only"
        return out

    rows = read_csv(raw_path)
    out["rows_in"] = len(rows)
    if not rows:
        out["note"] = "nothing to clean"
        return out

    clean = _load_clean(config)
    if clean is None:
        out["state"] = "error"
        out["note"] = "could not import clean.common from the survey tool"
        return out

    try:
        import pandas as pd
    except ImportError:                                   # pragma: no cover
        out["state"] = "error"
        out["note"] = "pandas is not installed"
        return out

    frame = pd.DataFrame(rows, dtype=object)
    chatter = io.StringIO()
    try:
        # The repo's steps print their row counts; keep that out of the console
        # and hand it to the caller instead.
        with contextlib.redirect_stdout(chatter):
            cleaned = clean(frame, survey, season)
    except Exception as exc:
        out["state"] = "error"
        out["note"] = f"{type(exc).__name__}: {exc}"
        out["log"] = chatter.getvalue().strip()
        return out

    records = cleaned.where(cleaned.notna(), "").to_dict("records")
    write_csv(clean_path(raw_path), records)
    out.update(state="done", rows_out=len(records), log=chatter.getvalue().strip())
    return out
