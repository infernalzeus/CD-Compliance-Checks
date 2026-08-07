#!/usr/bin/env python
"""Launch the CD-Compliance-Checks dashboard.

    python serve.py [--host 127.0.0.1] [--port 8000] [--reload]

Equivalent to `uvicorn webapp.server:app`. Config path defaults to
./config.yaml (override with the CDCC_CONFIG environment variable).

Ctrl+C stops the server: the first press cancels any running check (killing the
tool subprocesses and interrupting an in-progress OneDrive download) and shuts
down gracefully; a second press force-quits immediately. Without that, a Ctrl+C
during a ~1 GB hydration used to hang on "Waiting for background tasks to
complete" and needed the process killed by hand.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Seconds uvicorn may spend waiting for open connections before forcing the exit.
GRACEFUL_TIMEOUT = 8


def main() -> None:
    p = argparse.ArgumentParser(description="Serve the compliance dashboard.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    import uvicorn

    if args.reload:
        # The reloader supervises its own child process, so the custom signal
        # handling below doesn't apply; use the stock runner for dev reloads.
        uvicorn.run(
            "webapp.server:app",
            host=args.host,
            port=args.port,
            reload=True,
            app_dir=str(HERE),
            timeout_graceful_shutdown=GRACEFUL_TIMEOUT,
        )
        return

    from cdcompliance import tools

    class _Server(uvicorn.Server):
        """uvicorn server with a two-stage Ctrl+C."""

        def handle_exit(self, sig, frame):  # noqa: D102 - uvicorn hook
            if self.should_exit:
                # Second Ctrl+C: don't wait for anything.
                print("\nForce quit.", flush=True)
                try:
                    tools.terminate_all()
                except Exception:
                    pass
                os._exit(1)
            # Plain ASCII: Windows consoles are often cp1252 and mangle dashes.
            print(
                "\nStopping - cancelling any running checks "
                "(press Ctrl+C again to force quit)...",
                flush=True,
            )
            try:
                killed = tools.terminate_all()
                if killed:
                    print(f"  terminated {killed} tool subprocess(es)", flush=True)
            except Exception as exc:
                print(f"  could not terminate subprocesses: {exc}", flush=True)
            super().handle_exit(sig, frame)

    # No app_dir here (uvicorn.Config has no such option — it is a uvicorn.run /
    # CLI convenience); the sys.path insert at the top of this file does the job.
    config = uvicorn.Config(
        "webapp.server:app",
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=GRACEFUL_TIMEOUT,
    )
    try:
        _Server(config).run()
    except KeyboardInterrupt:
        # Expected: after a clean shutdown uvicorn re-raises the signal it
        # captured (Server.capture_signals), which surfaces here as a
        # KeyboardInterrupt. The server has already stopped — swallow it so the
        # console shows "Stopped." instead of a scary traceback.
        pass
    print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
