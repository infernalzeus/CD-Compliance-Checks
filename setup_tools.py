#!/usr/bin/env python
"""Fetch the external Step 1 / Step 2 tool repos from GitHub.

CD-Compliance-Checks runs two standalone tools as subprocesses:
  - Step 1  actigraphy-epoching        (.bin -> 60-second epoch CSV)
  - Step 2  actigraphy-sleep-metrics    (epoch CSV -> IS/IV/M10/L5/SRI + PDF)

They are not Python packages, so they can't go in requirements.txt directly.
Run this after installing requirements to clone them (or `git pull` the latest)
into the folders named in config.yaml, and install each tool's own requirements:

    pip install -r requirements.txt
    python setup_tools.py            # clone any that are missing
    python setup_tools.py --update   # also `git pull` the ones already present

Override where the repos live from the dashboard's ⚙ settings (or config.yaml:
tools.epoching_repo / tools.sleep_metrics_repo). GitHub sources come from
tools.epoching_repo_url / tools.sleep_metrics_repo_url.

Adding a new device tool later (e.g. luminosity): give it a repo + URL in
config and add an entry to `tool_specs()` below — the same clone/update logic
then fetches it on install.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from cdcompliance.config import load_config  # noqa: E402


def tool_specs(config) -> list[tuple[str, str, Path]]:
    """(name, github url, local path) for every external tool repo."""
    return [
        ("actigraphy-epoching", config.tools.epoching_repo_url, config.tools.epoching_repo),
        ("actigraphy-sleep-metrics", config.tools.sleep_metrics_repo_url, config.tools.sleep_metrics_repo),
    ]


def _run(args: list[str], cwd: Path | None = None) -> int:
    print("  $", " ".join(args))
    return subprocess.run(args, cwd=str(cwd) if cwd else None).returncode


def ensure_repo(name: str, url: str, path: Path, update: bool) -> None:
    path = Path(path)
    if (path / ".git").exists():
        if update:
            print(f"[{name}] updating {path}")
            _run(["git", "-C", str(path), "pull", "--ff-only"])
        else:
            print(f"[{name}] already present at {path}  (use --update to git pull)")
        return
    if path.exists() and any(path.iterdir()):
        print(f"[{name}] SKIP — {path} exists but is not a git repo (won't overwrite)")
        return
    print(f"[{name}] cloning {url}\n           -> {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", url, str(path)])


def install_requirements(name: str, path: Path, python: str) -> None:
    req = Path(path) / "requirements.txt"
    if req.exists():
        print(f"[{name}] installing its requirements")
        _run([python, "-m", "pip", "install", "-r", str(req)])


def main() -> int:
    p = argparse.ArgumentParser(description="Clone/update the Step 1 & 2 tool repos.")
    p.add_argument("--config", type=Path, default=_ROOT / "config.yaml")
    p.add_argument("--update", action="store_true", help="git pull repos that already exist")
    p.add_argument("--no-deps", action="store_true", help="skip installing the tools' requirements")
    args = p.parse_args()

    config = load_config(args.config)
    specs = tool_specs(config)

    for name, url, path in specs:
        ensure_repo(name, url, path, args.update)
    if not args.no_deps:
        for name, _url, path in specs:
            install_requirements(name, path, config.tools.python_executable)

    print("\nTool setup complete. Verify paths in the dashboard ⚙ settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
