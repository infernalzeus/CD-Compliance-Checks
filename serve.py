#!/usr/bin/env python
"""Launch the CD-Compliance-Checks dashboard.

    python serve.py [--host 127.0.0.1] [--port 8000] [--reload]

Equivalent to `uvicorn webapp.server:app`. Config path defaults to
./config.yaml (override with the CDCC_CONFIG environment variable).
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description="Serve the compliance dashboard.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    import uvicorn

    uvicorn.run(
        "webapp.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
