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

from cdcompliance import manifest, results, updates  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402
from cdcompliance.devices import all_devices, implemented_devices  # noqa: E402
from webapp.jobs import JobManager  # noqa: E402

CONFIG_PATH = Path(os.environ.get("CDCC_CONFIG", _ROOT / "config.yaml"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="CD-Compliance-Checks Dashboard")

_config = load_config(CONFIG_PATH)

# Per-install path overrides, editable from the UI and persisted across sessions.
_SETTINGS_PATH = Path(os.environ.get("CDCC_SETTINGS", _ROOT / "runtime_settings.json"))


def _apply_runtime_settings() -> None:
    if not _SETTINGS_PATH.exists():
        return
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if data.get("source_root"):
            _config.paths.source_root = Path(data["source_root"])
        if data.get("output_root"):
            _config.paths.output_root = Path(data["output_root"])
        if data.get("epoching_repo"):
            _config.tools.epoching_repo = Path(data["epoching_repo"])
        if data.get("sleep_metrics_repo"):
            _config.tools.sleep_metrics_repo = Path(data["sleep_metrics_repo"])
        if data.get("luminosity_repo"):
            _config.tools.luminosity_repo = Path(data["luminosity_repo"])
        if data.get("expiwell_repo"):
            _config.tools.expiwell_repo = Path(data["expiwell_repo"])
    except Exception as exc:  # bad settings file must not stop startup
        print(f"[settings] could not load {_SETTINGS_PATH}: {exc}")


_apply_runtime_settings()
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
    if not key:
        return JSONResponse({"ok": False, "error": "no component key"}, status_code=400)
    return JSONResponse(updates.update(_config, key))


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
    try:
        _SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "source_root": str(_config.paths.source_root),
                    "output_root": str(_config.paths.output_root),
                    "epoching_repo": str(_config.tools.epoching_repo),
                    "sleep_metrics_repo": str(_config.tools.sleep_metrics_repo),
                    "luminosity_repo": str(_config.tools.luminosity_repo),
                    "expiwell_repo": str(_config.tools.expiwell_repo),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        print(f"[settings] could not persist: {exc}")
        return False


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
    def _clean(key):
        return (payload.get(key) or "").strip().strip('"')

    src, out = _clean("source_root"), _clean("output_root")
    e1, e2 = _clean("epoching_repo"), _clean("sleep_metrics_repo")
    e3, e4 = _clean("luminosity_repo"), _clean("expiwell_repo")
    if src:
        _config.paths.source_root = Path(src)
    if out:
        _config.paths.output_root = Path(out)
    if e1:
        _config.tools.epoching_repo = Path(e1)
    if e2:
        _config.tools.sleep_metrics_repo = Path(e2)
    if e3:
        _config.tools.luminosity_repo = Path(e3)
    if e4:
        _config.tools.expiwell_repo = Path(e4)
    saved = True
    try:
        _SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "source_root": str(_config.paths.source_root),
                    "output_root": str(_config.paths.output_root),
                    "epoching_repo": str(_config.tools.epoching_repo),
                    "sleep_metrics_repo": str(_config.tools.sleep_metrics_repo),
                    "luminosity_repo": str(_config.tools.luminosity_repo),
                    "expiwell_repo": str(_config.tools.expiwell_repo),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        saved = False
        print(f"[settings] could not persist: {exc}")
    result = _settings_payload()
    result["persisted"] = saved
    return JSONResponse(result)


def _settings_payload() -> dict:
    return {
        "source_root": str(_config.paths.source_root),
        "output_root": str(_config.paths.output_root),
        "source_exists": _config.paths.source_root.exists(),
        "output_exists": _config.paths.output_root.exists(),
        "epoching_repo": str(_config.tools.epoching_repo),
        "sleep_metrics_repo": str(_config.tools.sleep_metrics_repo),
        "epoching_exists": _config.tools.epoching_repo.exists(),
        "sleep_metrics_exists": _config.tools.sleep_metrics_repo.exists(),
        "luminosity_repo": str(_config.tools.luminosity_repo),
        "expiwell_repo": str(_config.tools.expiwell_repo),
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
    job = _jobs.submit(participants, force, dry_run)
    return JSONResponse({"job_id": job.id, "job": job.info()})


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
