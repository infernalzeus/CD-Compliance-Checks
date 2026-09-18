"""FastAPI application for the CD-Compliance-Checks dashboard.

Serves the static single-page UI and a small JSON/WebSocket API over the
`cdcompliance` core:

    GET  /                                  -> dashboard page
    GET  /api/config                        -> paths + implemented devices
    GET  /api/panel1/grid                   -> source (staging) grid model
    GET  /api/panel2/grid                   -> output grid model
    GET  /api/participant/{pid}/source      -> staging file tree ("WT Onedrive")
    POST /api/run   {participants, force}   -> start a job, returns {job_id}
    GET  /api/jobs/{job_id}                 -> job status
    WS   /ws/jobs/{job_id}                  -> live progress event stream
    POST /api/panel2/aggregate {participants} -> combined compliance numbers
    GET  /api/participant/{pid}/measures?season=&device=&stem= -> Step 2 measures

Launch:
    uvicorn webapp.server:app --reload      (from the project root)
    # or:  python serve.py
Config path defaults to <project>/config.yaml; override with env CDCC_CONFIG.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from cdcompliance import batches, manifest, naming, results, updates  # noqa: E402
from cdcompliance import pseudo_id, t2_data, t2_selections, user_settings  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402
from cdcompliance.devices import all_devices, implemented_devices  # noqa: E402
from webapp.jobs import JobManager  # noqa: E402

CONFIG_PATH = Path(os.environ.get("CDCC_CONFIG", _ROOT / "config.yaml"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="CD-Compliance-Checks Dashboard")

_config = load_config(CONFIG_PATH)

# Folder choices from the ⚙ panel, saved per user (see cdcompliance.user_settings).
# A settings file left in the app folder by older versions is migrated once.
_LEGACY_SETTINGS = _ROOT / "runtime_settings.json"
_SETTINGS_PATH = user_settings.apply(_config, legacy_file=_LEGACY_SETTINGS)


_jobs = JobManager(_config)


def _device_folder(device: str) -> str:
    key = device.lower()
    if key == "actigraph":
        return _config.actigraph_folder_name
    if key == "mieye":
        return _config.mieye_folder_name
    try:
        from cdcompliance.devices import get_processor

        folder = get_processor(key).folder_name
        if folder:
            return folder
    except Exception:
        pass
    return device.capitalize()


def _safe_output_path(participant: str, season: str, device: str, name: str):
    """Resolve an output file, refusing anything outside the output tree."""
    base = _config.paths.output_root.resolve()
    target = (base / participant / season / _device_folder(device) / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


# Set once the server starts shutting down, so open WebSocket handlers stop
# waiting on their queue and return. Without this, Ctrl+C hung in "Waiting for
# background tasks to complete" until the browser tab was closed (and dumped a
# CancelledError traceback from `await queue.get()`).
_shutting_down = asyncio.Event()


@app.on_event("startup")
async def _startup() -> None:
    _jobs.bind_loop(asyncio.get_running_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Ctrl+C / graceful stop: cancel work before the loop is torn down.

    Order matters — flag the jobs first (so an in-flight OneDrive download or
    tool subprocess is actually interrupted), then release the WebSocket
    handlers so uvicorn's graceful shutdown has nothing left to wait on.
    """
    _shutting_down.set()
    try:
        killed = _jobs.stop_all()
        if killed:
            print(f"[shutdown] terminated {killed} tool subprocess(es)")
    except Exception as exc:  # shutdown must never raise
        print(f"[shutdown] job teardown failed: {exc}")
    _jobs.release_subscribers()


@app.middleware("http")
async def _no_cache(request, call_next):
    """Serve the page and static assets with no caching so edits always apply
    (avoids the browser holding onto a stale app.js/styles.css)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Static page
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def api_config() -> JSONResponse:
    return JSONResponse(
        {
            "source_root": str(_config.paths.source_root),
            "output_root": str(_config.paths.output_root),
            "participant_default": _config.participant,
            "devices": _config.devices,
            "implemented_devices": implemented_devices(),
            "all_devices": all_devices(),
        }
    )


# ---------------------------------------------------------------------------
# Component versions (startup update panel)
# ---------------------------------------------------------------------------
@app.get("/api/components")
def api_components(fetch: int = 1) -> JSONResponse:
    """Version status of this app and every linked tool repo.

    Sync (threadpool): `git fetch` touches the network and must not block the
    event loop. `fetch=0` gives the fast, offline, local-only answer.
    """
    return JSONResponse({
        "git": updates.git_available(),
        "components": updates.check_all(_config, fetch=bool(fetch)),
    })


@app.post("/api/components/update")
def api_component_update(payload: dict) -> JSONResponse:
    key = (payload or {}).get("key", "")
    keep_local = bool((payload or {}).get("keep_local", False))
    if not key:
        return JSONResponse({"ok": False, "error": "no component key"}, status_code=400)
    return JSONResponse(updates.update(_config, key, keep_local=keep_local))


@app.post("/api/components/install")
def api_component_install(payload: dict) -> JSONResponse:
    """Clone a tool repo into a user-chosen folder, then remember that folder."""
    key = (payload or {}).get("key", "")
    path = (payload or {}).get("path", "") or ""
    if not key:
        return JSONResponse({"ok": False, "error": "no component key"}, status_code=400)
    result = updates.install(_config, key, path or None)
    if result.get("ok") and path:
        attr = {
            "actigraphy-epoching": "epoching_repo",
            "actigraphy-sleep-metrics": "sleep_metrics_repo",
            "luminosity-metrics": "luminosity_repo",
            "expiwell-metrics": "expiwell_repo",
        }.get(key)
        if attr:
            setattr(_config.tools, attr, Path(path))
            _persist_settings()
    return JSONResponse(result)


def _persist_settings() -> bool:
    return user_settings.save(_config)


@app.post("/api/components/path")
def api_component_path(payload: dict) -> JSONResponse:
    """Point a component at an existing folder without cloning."""
    key = (payload or {}).get("key", "")
    path = ((payload or {}).get("path") or "").strip().strip('"')
    attr = {
        "actigraphy-epoching": "epoching_repo",
        "actigraphy-sleep-metrics": "sleep_metrics_repo",
        "luminosity-metrics": "luminosity_repo",
        "expiwell-metrics": "expiwell_repo",
    }.get(key)
    if not attr or not path:
        return JSONResponse({"ok": False, "error": "unknown component or empty path"},
                            status_code=400)
    setattr(_config.tools, attr, Path(path))
    saved = _persist_settings()
    spec = next((x for x in updates.component_specs(_config) if x["key"] == key), None)
    return JSONResponse({"ok": True, "persisted": saved,
                         "status": updates.status(spec, fetch=False) if spec else None})


@app.get("/api/settings")
def api_get_settings() -> JSONResponse:
    # sync (threadpool): .exists() checks may touch on-demand OneDrive paths.
    return JSONResponse(_settings_payload())


@app.post("/api/settings")
def api_set_settings(payload: dict) -> JSONResponse:
    user_settings.update(_config, payload or {})
    result = _settings_payload()
    result["persisted"] = _persist_settings()
    return JSONResponse(result)


def _settings_payload() -> dict:
    p, t = _config.paths, _config.tools
    return {
        "source_root": str(p.source_root),
        "output_root": str(p.output_root),
        "t2_root": str(p.t2_root),
        "source_exists": p.source_root.exists(),
        "output_exists": p.output_root.exists(),
        "t2_exists": p.t2_root.exists(),
        "epoching_repo": str(t.epoching_repo),
        "sleep_metrics_repo": str(t.sleep_metrics_repo),
        "epoching_exists": t.epoching_repo.exists(),
        "sleep_metrics_exists": t.sleep_metrics_repo.exists(),
        "luminosity_repo": str(t.luminosity_repo),
        "expiwell_repo": str(t.expiwell_repo),
        # Shown in the ⚙ panel so people know their choices are theirs alone.
        "settings_file": str(user_settings.settings_path()),
        "settings_saved_at": user_settings.saved_at(),
        "settings_error": user_settings.last_error,
        "initials": user_settings.get_text("initials"),
    }


@app.get("/api/output-file")
def api_output_file(participant: str, season: str, device: str, name: str):
    target = _safe_output_path(participant, season, device, name)
    if not target or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(target), content_disposition_type="inline")


@app.get("/api/output-csv")
def api_output_csv(participant: str, season: str, device: str, name: str) -> JSONResponse:
    target = _safe_output_path(participant, season, device, name)
    if not target or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    import numpy as np
    import pandas as pd
    try:
        df = pd.read_csv(target).replace({np.nan: None})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({
        "columns": [str(c) for c in df.columns],
        "rows": df.head(3000).to_dict(orient="records"),
        "truncated": len(df) > 3000,
    })


# NOTE: the folder-scanning endpoints below are plain `def` (not `async def`) on
# purpose. FastAPI runs sync path operations in a threadpool, so a slow scan of a
# large / on-demand OneDrive tree does NOT block the event loop — the settings
# panel, paths and static assets stay responsive while the grid loads.
@app.get("/api/panel1/grid")
def api_panel1_grid() -> JSONResponse:
    return JSONResponse({"cells": manifest.source_grid(_config)})


@app.get("/api/panel2/grid")
def api_panel2_grid() -> JSONResponse:
    # Panel 2 is coloured by compliance (green/red/orange), not processing state.
    return JSONResponse({"cells": results.panel2_grid(_config)})


def _grid_stream(which: str):
    """Yield NDJSON: one 'total' line, one 'cell' per participant, then 'done'.

    Streaming lets the UI fill the grid progressively and show an X / N meter
    instead of a blank screen while a large OneDrive tree is scanned. Sync
    generator -> Starlette iterates it in a threadpool (event loop stays free).
    """
    root = _config.paths.source_root if which == "source" else _config.paths.output_root
    try:
        names = manifest.list_cd_participants(root)
    except Exception:
        names = []
    yield json.dumps({"type": "total", "total": len(names)}) + "\n"
    for name in names:
        try:
            if which == "source":
                cell = manifest.participant_status(_config, name).to_dict()
                # Naming check rides along with the scan (it only lists folders),
                # so every cell arrives already knowing its flag counts.
                cell["flags"] = naming.summarise(naming.check_participant(_config, name))
            else:
                cell = results.panel2_cell(_config, name)
        except Exception as exc:  # one bad folder must not kill the stream
            cell = {"participant": name, "suffix": name, "state": "empty",
                    "done": 0, "total": 0, "error": str(exc)}
        yield json.dumps({"type": "cell", "cell": cell}) + "\n"
    yield json.dumps({"type": "done"}) + "\n"


@app.get("/api/panel1/grid-stream")
def api_panel1_grid_stream() -> StreamingResponse:
    return StreamingResponse(_grid_stream("source"), media_type="application/x-ndjson")


@app.get("/api/panel2/grid-stream")
def api_panel2_grid_stream() -> StreamingResponse:
    return StreamingResponse(_grid_stream("output"), media_type="application/x-ndjson")


@app.get("/api/participant/{pid}/flags")
def api_participant_flags(pid: str) -> JSONResponse:
    """Naming-convention flags for one participant's input files."""
    flags = naming.check_participant(_config, pid)
    return JSONResponse({"participant": pid, "counts": naming.summarise(flags),
                         "flags": flags})


@app.get("/api/participant/{pid}/naming")
def api_participant_naming(pid: str) -> JSONResponse:
    """Expected naming convention beside each current filename, per season/device."""
    return JSONResponse(naming.describe_participant(_config, pid))


@app.post("/api/flags/summary")
def api_flags_summary(payload: dict) -> JSONResponse:
    """Flags across several participants (Panel 1 CHECK, Panel 2 aggregate note)."""
    participants = [p for p in (payload or {}).get("participants", []) if p]
    per: dict = {}
    every: list = []
    for pid in participants:
        flags = naming.check_participant(_config, pid)
        per[pid] = naming.summarise(flags)
        every.extend(flags)
    return JSONResponse({"counts": naming.summarise(every), "per_participant": per,
                         "flags": every})


@app.get("/api/participant/{pid}/source")
def api_source_tree(pid: str) -> JSONResponse:
    return JSONResponse(results.source_tree(_config, pid))


@app.post("/api/run")
async def api_run(payload: dict) -> JSONResponse:
    participants = [p for p in payload.get("participants", []) if p]
    force = bool(payload.get("force", False))
    dry_run = bool(payload.get("dry_run", False))
    if not participants:
        return JSONResponse({"error": "no participants selected"}, status_code=400)
    # Any run that writes data is a recorded batch and must say who ran it.
    initials = batches.normalise_initials(payload.get("initials"))
    if not dry_run and initials is None:
        return JSONResponse({"error": "initials required (2-4 letters) for a run that writes data"},
                            status_code=400)
    if initials:
        user_settings.set_text("initials", initials)   # remembered for this user
        _persist_settings()
    job = _jobs.submit(participants, force, dry_run, initials or "")
    return JSONResponse({"job_id": job.id, "job": job.info()})


@app.get("/api/batches")
def api_batches(archived: int = 0, stage: str = "PRE") -> JSONResponse:
    """Batches of one stage, newest first (public stripped records)."""
    return JSONResponse({"batches": batches.list_batches(_config, include_archived=bool(archived),
                                                         stage=stage or None)})


@app.post("/api/batches/{batch_id}/archive")
def api_batch_archive(batch_id: str, payload: dict) -> JSONResponse:
    """Hide a batch from the list; both records are kept with who/when."""
    try:
        rec = batches.archive_batch(_config, batch_id, (payload or {}).get("initials", ""))
    except FileNotFoundError:
        return JSONResponse({"error": "batch not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "archived": rec.get("archived")})


# ---------------------------------------------------------------------------
# T2: Visualise -> T2 hand-off (writes a record only) and read-only preview
# ---------------------------------------------------------------------------
_PID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,32}$")


def _t2_payload(payload: dict) -> dict:
    payload = dict(payload or {})
    payload["participants"] = [p for p in payload.get("participants", []) if p and _PID_RE.match(p)]
    return payload


@app.get("/api/t2/dictionary")
def api_t2_dictionary() -> JSONResponse:
    """Every variable T2 can select, with unit, definition and timing."""
    return JSONResponse({"variables": t2_data.variables(), "checks": t2_selections.USER_CHECKS})


@app.get("/api/privacy")
def api_privacy() -> JSONResponse:
    """Whether pseudonymous IDs are on (never returns the key)."""
    return JSONResponse(pseudo_id.status(_config))


@app.get("/api/participant/{pid}/seasons")
def api_participant_seasons(pid: str) -> JSONResponse:
    """Season folders numbered 1..n by the dates their data covers."""
    if not _PID_RE.match(pid):
        return JSONResponse({"error": "invalid participant"}, status_code=400)
    return JSONResponse({"participant": pid, "seasons": t2_data.season_numbers(_config, pid)})


@app.post("/api/t2/seasons")
def api_t2_seasons(payload: dict) -> JSONResponse:
    """The calendar seasons these participants have data in, with counts.

    Keyed on the season of the year the recordings fall in - folder labels
    disagree between participants, and one person's 2nd season is another's 3rd.
    """
    seasons: dict = {}
    numbers: dict = {}
    for pid in _t2_payload(payload)["participants"]:
        for folder, info in t2_data.season_numbers(_config, pid).items():
            numbers[info["n"]] = numbers.get(info["n"], 0) + 1
            key = info.get("season_key")
            if not key:
                continue
            entry = seasons.setdefault(key, {"key": key, "label": info.get("season_label"),
                                             "code": info.get("season_code"), "n_participants": 0,
                                             "estimated": 0, "folders": []})
            entry["n_participants"] += 1
            if info.get("basis") != "data":
                entry["estimated"] += 1
            if folder not in entry["folders"]:
                entry["folders"].append(folder)
    return JSONResponse({
        "seasons": sorted(seasons.values(), key=lambda e: e["key"]),
        "season_counts": {str(k): v for k, v in sorted(numbers.items())},
    })


@app.post("/api/t2/availability")
def api_t2_availability(payload: dict) -> JSONResponse:
    """Cross-device check between Visualise and T2 (no data values returned)."""
    sel = t2_selections.normalise(_t2_payload(payload))
    if not sel.participants:
        return JSONResponse({"error": "no participants selected"}, status_code=400)
    try:
        return JSONResponse(t2_selections.summary(_config, sel, for_handoff=True))
    except pseudo_id.PseudoIdError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/t2/selections")
def api_t2_send(payload: dict) -> JSONResponse:
    """Send a Visualise selection to T2: records it in runs/, writes no data."""
    try:
        public = t2_selections.create(_config, _t2_payload(payload))
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    initials = batches.normalise_initials((payload or {}).get("initials"))
    if initials:
        user_settings.set_text("initials", initials)
        _persist_settings()
    return JSONResponse({"ok": True, "selection": public})


@app.get("/api/t2/selections")
def api_t2_selections(archived: int = 0) -> JSONResponse:
    return JSONResponse({"selections": t2_selections.list_selections(_config, bool(archived))})


@app.get("/api/t2/selections/{batch_id}")
def api_t2_selection(batch_id: str) -> JSONResponse:
    """A selection's definition plus a fresh availability summary."""
    try:
        record, sel = t2_selections.load_selection(_config, batch_id)
        info = t2_selections.summary(_config, sel)
    except FileNotFoundError:
        return JSONResponse({"error": "no local detail for this selection (sent from another computer?)"},
                            status_code=404)
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    head = {k: record.get(k) for k in ("batch_id", "name", "initials", "started_at", "seasons",
                                       "devices", "variables", "status", "checks")}
    return JSONResponse({"record": head, "summary": info,
                         "same_unit": t2_selections.same_unit_pairs(sel)})


@app.post("/api/t2/selections/{batch_id}/archive")
def api_t2_selection_archive(batch_id: str, payload: dict) -> JSONResponse:
    t2_selections.forget(batch_id)
    return api_batch_archive(batch_id, payload)


@app.post("/api/t2/timeline")
def api_t2_timeline(payload: dict) -> JSONResponse:
    """Day-level series for one participant-season of a selection (preview only)."""
    payload = payload or {}
    try:
        _record, sel = t2_selections.load_selection(_config, str(payload.get("selection", "")))
        data = t2_selections.timeline(_config, sel, str(payload.get("display_id", "")),
                                      int(payload.get("season_n", 0)))
    except FileNotFoundError:
        return JSONResponse({"error": "selection not found"}, status_code=404)
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data)


def _t2_view(payload: dict, fn):
    payload = payload or {}
    batch_id = str(payload.get("selection", ""))
    try:
        _record, sel = t2_selections.load_selection(_config, batch_id)
        return JSONResponse(fn(_config, batch_id, sel, payload))
    except FileNotFoundError:
        return JSONResponse({"error": "selection not found"}, status_code=404)
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/t2/scatter")
def api_t2_scatter(payload: dict) -> JSONResponse:
    """Cross-device scatter + within/between correlations (preview only)."""
    return _t2_view(payload, t2_selections.scatter)


@app.post("/api/t2/matrix")
def api_t2_matrix(payload: dict) -> JSONResponse:
    """Correlation matrix across the chosen variables, FDR-corrected."""
    return _t2_view(payload, t2_selections.matrix)


@app.post("/api/t2/cutoffs")
def api_t2_cutoffs(payload: dict) -> JSONResponse:
    """Slider bounds per measure, and what the current cut-offs leave behind."""
    return _t2_view(payload, t2_selections.cutoff_options)


@app.post("/api/t2/export/preview")
def api_t2_export_preview(payload: dict) -> JSONResponse:
    """Exactly which files would be written, before anything is written."""
    return _t2_view(payload, t2_selections.export_preview)


@app.post("/api/t2/export")
def api_t2_export(payload: dict) -> JSONResponse:
    """Write the dataset. The only step in T2 that produces files."""
    return _t2_view(payload, t2_selections.export_write)


@app.post("/api/t2/overlap")
def api_t2_overlap(payload: dict) -> JSONResponse:
    """Do two same-unit measures land on the same value each day?"""
    return _t2_view(payload, t2_selections.overlap)


@app.post("/api/t2/compare")
def api_t2_compare(payload: dict) -> JSONResponse:
    """Compare one variable across groups, calendar seasons or participants."""
    return _t2_view(payload, t2_selections.compare)


@app.get("/api/t2/selections/{batch_id}/groups")
def api_t2_groups(batch_id: str) -> JSONResponse:
    try:
        record, sel = t2_selections.load_selection(_config, batch_id)
        ids = pseudo_id.mapping(_config, sel.participants)
    except FileNotFoundError:
        return JSONResponse({"error": "selection not found"}, status_code=404)
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    groups = record.get("groups") or {}
    return JSONResponse({
        "groups": {name: [ids.get(p, p) for p in members] for name, members in groups.items()},
        "participants": [ids.get(p, p) for p in sel.participants],
    })


@app.post("/api/t2/selections/{batch_id}/groups")
def api_t2_set_groups(batch_id: str, payload: dict) -> JSONResponse:
    """Save the groups made in T2 (local record only, never the public one)."""
    try:
        groups = t2_selections.set_groups(_config, batch_id, (payload or {}).get("groups") or {})
    except FileNotFoundError:
        return JSONResponse({"error": "selection not found"}, status_code=404)
    except (t2_selections.SelectionError, pseudo_id.PseudoIdError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    ids = pseudo_id.mapping(_config, [p for members in groups.values() for p in members])
    return JSONResponse({"ok": True,
                         "groups": {n: [ids.get(p, p) for p in m] for n, m in groups.items()}})


@app.post("/api/t2/seasons-summary")
def api_t2_seasons_summary(payload: dict) -> JSONResponse:
    """Per-season summary of each variable (means of participant means)."""
    return _t2_view(payload, t2_selections.seasons_view)


@app.post("/api/shutdown")
async def api_shutdown() -> JSONResponse:
    """Stop the server (and free the port). Triggered by the top-right ✕ button.

    Kills any running tool subprocesses first so nothing is left orphaned.
    """
    killed = _jobs.stop_all()

    def _stop() -> None:
        time.sleep(0.4)  # let this response flush first
        # Give a cancelled run a moment to unwind (close the .bin it was
        # hydrating, delete a partial copy) before pulling the plug.
        _jobs.wait_idle(timeout=5.0)
        os._exit(0)

    threading.Thread(target=_stop, daemon=True).start()
    return JSONResponse({"stopping": True, "killed_processes": killed})


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str) -> JSONResponse:
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.info())


@app.post("/api/jobs/{job_id}/stop")
async def api_stop(job_id: str) -> JSONResponse:
    ok = _jobs.stop(job_id)
    if not ok:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({"stopping": True})


@app.get("/api/participant/{pid}/output-items")
def api_output_items(pid: str) -> JSONResponse:
    return JSONResponse({"items": results.output_items(_config, pid)})


@app.post("/api/panel2/aggregate")
def api_aggregate(payload: dict) -> JSONResponse:
    participants = [p for p in payload.get("participants", []) if p]
    return JSONResponse(results.aggregate(_config, participants))


@app.get("/api/participant/{pid}/measures")
def api_measures(pid: str, season: str, device: str, stem: str) -> JSONResponse:
    return JSONResponse(results.item_measures(_config, pid, season, device, stem))


# ---------------------------------------------------------------------------
# WebSocket progress stream
# ---------------------------------------------------------------------------
async def _await_disconnect(websocket: WebSocket) -> None:
    """Return as soon as the socket goes away (client close or server shutdown).

    Uvicorn closes open connections *before* running the lifespan shutdown hook,
    so this — not the shutdown event — is what lets a streaming handler notice
    Ctrl+C promptly. Without it the handler sat in `await queue.get()` until the
    graceful-shutdown timeout expired and cancelled it.
    """
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    job = _jobs.get(job_id)
    if not job:
        await websocket.send_json({"type": "error", "message": "unknown job"})
        await websocket.close()
        return

    queue: asyncio.Queue = asyncio.Queue()
    job.subscribers.append(queue)
    last_seq = 0
    disconnected = asyncio.ensure_future(_await_disconnect(websocket))
    getter: Optional[asyncio.Future] = None
    try:
        # Replay backlog first.
        for event in list(job.history):
            await websocket.send_json(event)
            last_seq = max(last_seq, event.get("_seq", 0))
        # Then stream new events (skipping any already replayed), racing each
        # wait against the socket closing so shutdown is never blocked on us.
        while not _shutting_down.is_set():
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait(
                {getter, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done:
                getter.cancel()
                break
            event = getter.result()
            # `None` is the shutdown sentinel pushed by _shutdown().
            if event is None:
                break
            if event.get("_seq", 0) <= last_seq:
                continue
            last_seq = event["_seq"]
            await websocket.send_json(event)
            if event.get("type") == "job_end":
                break
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # The server is going down and cancelled this handler. Exit quietly —
        # re-raising just prints an "Exception in ASGI application" traceback.
        pass
    except RuntimeError:
        # Socket already closed underneath us.
        pass
    finally:
        if queue in job.subscribers:
            job.subscribers.remove(queue)
        disconnected.cancel()
        if getter is not None:
            getter.cancel()
        try:
            await websocket.close()
        except (RuntimeError, asyncio.CancelledError, WebSocketDisconnect):
            pass


# Mount static assets last so /api and / take precedence.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
