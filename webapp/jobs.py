"""Background job manager bridging the pipeline's EventBus to WebSockets.

`pipeline.run()` is synchronous and emits events on an `EventBus`. Jobs run in a
worker thread; each emitted event is (a) appended to the job's history buffer and
(b) pushed to every connected WebSocket subscriber via the main asyncio loop.
Each event is tagged with a monotonic `_seq` so a late-connecting client can
receive the backlog and then stream new events without duplicates.

Runs are queued and executed one at a time (single worker), matching the local
single-user model and avoiding two heavy downloads/processes competing.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any, Optional

from cdcompliance import batches, naming, pipeline, tools
from cdcompliance.config import Config, ResolvedSelection
from cdcompliance.devices import implemented_devices
from cdcompliance.events import EventBus


class Job:
    def __init__(
        self, job_id: str, participants: list[str], force: bool, dry_run: bool = False,
        initials: str = "",
    ) -> None:
        self.id = job_id
        self.participants = participants
        self.force = force
        self.dry_run = dry_run
        self.initials = initials
        self.batch_id: Optional[str] = None
        self.recorder = None      # BatchRecorder for real runs
        self.status = "queued"  # queued | running | done | error | cancelled
        self.history: list[dict[str, Any]] = []
        self.subscribers: list[asyncio.Queue] = []
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.cancel_event = threading.Event()
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "participants": self.participants,
            "force": self.force,
            "dry_run": self.dry_run,
            "status": self.status,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "events": len(self.history),
        }


class JobManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.jobs: dict[str, Job] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "Queue[Job]" = Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- public API ----------------------------------------------------------
    def submit(self, participants: list[str], force: bool, dry_run: bool = False,
               initials: str = "") -> Job:
        job = Job(uuid.uuid4().hex[:12], participants, force, dry_run, initials)
        self.jobs[job.id] = job
        if dry_run:
            # Dry runs are read-only and fast — run immediately in their own
            # thread so they are never blocked behind a long real run.
            threading.Thread(target=self._run_job_safe, args=(job,), daemon=True).start()
        else:
            self._queue.put(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        """Cancel a running job: flag it and kill its current tool subprocess."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel_event.set()
        killed = tools.terminate_all()
        self._emit(
            job,
            {
                "type": "log",
                "level": "warning",
                "message": f"Stop requested — cancelling ({killed} process(es) terminated).",
            },
        )
        return True

    def stop_all(self) -> int:
        for job in self.jobs.values():
            job.cancel_event.set()
        return tools.terminate_all()

    def release_subscribers(self) -> None:
        """Push the shutdown sentinel to every open WebSocket queue.

        Called on server shutdown so handlers blocked in `await queue.get()`
        return instead of keeping uvicorn's graceful shutdown waiting.
        """
        for job in self.jobs.values():
            for q in list(job.subscribers):
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait (briefly) for the running job to unwind after a cancel.

        Returns True if no job is still running. Used on shutdown so the process
        exits after the pipeline has actually stopped rather than while it is
        still mid-download.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(job.status == "running" for job in self.jobs.values()):
                return True
            time.sleep(0.1)
        return not any(job.status == "running" for job in self.jobs.values())

    # -- worker --------------------------------------------------------------
    def _run_worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._run_job_safe(job)
            finally:
                self._queue.task_done()

    def _run_job_safe(self, job: Job) -> None:
        try:
            self._execute(job)
        except Exception as exc:  # never let a job crash the worker/thread
            self._emit(job, {"type": "log", "level": "error", "message": str(exc)})
            job.status = "error"
            if job.recorder is not None:          # still record the failed batch
                try:
                    job.recorder.finish("error")
                except Exception:
                    pass
                job.recorder = None
            self._emit(
                job,
                {"type": "job_end", "status": "error", "error": str(exc), "dry_run": job.dry_run},
            )

    def _execute(self, job: Job) -> None:
        job.status = "running"
        bus = EventBus()
        bus.subscribe(lambda event: self._emit(job, event))

        self._emit(
            job,
            {
                "type": "job_start",
                "participants": job.participants,
                "force": job.force,
                "dry_run": job.dry_run,
            },
        )

        # A real run is one batch (stage 1: staging -> master), recorded as a
        # public stripped record + private detail. Previews write nothing.
        recorder = None
        if not job.dry_run:
            recorder = batches.BatchRecorder(
                self.config, stage="PRE", mode="force" if job.force else "new",
                initials=job.initials, participants=job.participants,
            )
            job.batch_id = recorder.id
            job.recorder = recorder
            bus.subscribe(recorder)
            self._emit(job, {"type": "batch_start", "batch_id": recorder.id,
                             "initials": recorder.initials})

        for pid in job.participants:
            if job.cancel_event.is_set():
                break
            selection = ResolvedSelection(
                participant=pid,
                seasons=None,
                # Run every implemented check (matches the grid, which is coloured
                # by all-device completeness). Already-complete items are skipped,
                # and a device with no input for this participant is a no-op.
                devices=implemented_devices(),
                copy_bin=self.config.copy_bin,
                dry_run=job.dry_run,
                force=job.force,
            )
            if job.dry_run:
                # Preview only: discovery + OneDrive state, no download/processing.
                pipeline.plan(self.config, selection, bus)
            else:
                result = pipeline.run(self.config, selection, bus,
                                      cancel_event=job.cancel_event)
                try:
                    flags = naming.summarise(naming.check_participant(self.config, pid))
                except Exception:
                    flags = None
                recorder.add_result(result, flags)

        job.status = "cancelled" if job.cancel_event.is_set() else "done"
        if recorder is not None:
            job.recorder = None
            record = recorder.finish(job.status)
            self._emit(job, {"type": "batch_end", "batch_id": recorder.id,
                             "items": record["items"]})
        self._emit(
            job,
            {"type": "job_end", "status": job.status, "dry_run": job.dry_run},
        )

    # -- event fan-out -------------------------------------------------------
    def _emit(self, job: Job, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        event["_seq"] = job.next_seq()
        job.history.append(event)
        loop = self._loop
        if loop is None:
            return
        for q in list(job.subscribers):
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass
