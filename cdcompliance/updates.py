"""Component version checks against the linked GitHub repositories.

CD-Compliance-Checks is one repo plus a set of standalone tool repos it invokes
(actigraphy-epoching, actigraphy-sleep-metrics, luminosity-metrics, and later
expiwell-metrics). This module reports, for each of them, whether the local
checkout is behind its GitHub remote, and can fast-forward it.

Everything degrades gracefully: a missing ``git``, a folder that is not a git
checkout (e.g. downloaded as a ZIP), or no network simply produces a status the
UI can display rather than an error.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from .config import Config

#: How long any single git call may take before we give up (seconds).
GIT_TIMEOUT = 25


def _git(args: list[str], cwd: Optional[Path] = None, timeout: int = GIT_TIMEOUT):
    """Run a git command, returning (ok, stdout). Never raises."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)


def git_available() -> bool:
    ok, _ = _git(["--version"], timeout=10)
    return ok


def component_specs(config: Config) -> list[dict[str, Any]]:
    """The app itself plus every external tool repo it drives."""
    root = Path(__file__).resolve().parent.parent
    specs = [
        {"key": "cd-compliance-checks", "name": "CD-Compliance-Checks",
         "role": "dashboard (this app)", "path": root, "url": ""},
        {"key": "actigraphy-epoching", "name": "actigraphy-epoching",
         "role": "Actigraph step 1 - epoching",
         "path": Path(config.tools.epoching_repo), "url": config.tools.epoching_repo_url},
        {"key": "actigraphy-sleep-metrics", "name": "actigraphy-sleep-metrics",
         "role": "Actigraph step 2 - sleep metrics",
         "path": Path(config.tools.sleep_metrics_repo),
         "url": config.tools.sleep_metrics_repo_url},
    ]
    for attr, key, role in (
        ("luminosity_repo", "luminosity-metrics", "MiEYE - luminosity"),
        ("expiwell_repo", "expiwell-metrics", "Expiwell - ESM compliance"),
    ):
        path = getattr(config.tools, attr, None)
        if path is None:
            continue
        specs.append({
            "key": key, "name": key, "role": role, "path": Path(path),
            "url": getattr(config.tools, f"{attr}_url", "") or "",
        })
    return specs


def status(spec: dict[str, Any], fetch: bool = True) -> dict[str, Any]:
    """Local vs remote state for one component."""
    path = Path(spec["path"])
    out: dict[str, Any] = {
        "key": spec["key"], "name": spec["name"], "role": spec.get("role", ""),
        "path": str(path), "url": spec.get("url", ""),
        "exists": path.exists(), "is_git": False, "branch": None, "commit": None,
        "behind": 0, "ahead": 0, "update_available": False,
        "dirty": False, "state": "unknown", "message": "",
    }
    if not path.exists():
        out["state"] = "missing"
        out["message"] = "not installed - run setup_tools.py"
        return out
    if not (path / ".git").exists():
        out["state"] = "not-git"
        out["message"] = "not a git checkout, cannot auto-update"
        return out

    out["is_git"] = True
    ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    out["branch"] = branch if ok else None
    ok, commit = _git(["rev-parse", "--short", "HEAD"], path)
    out["commit"] = commit if ok else None
    ok, dirty = _git(["status", "--porcelain"], path)
    out["dirty"] = bool(ok and dirty)

    if fetch:
        fok, fmsg = _git(["fetch", "--quiet"], path)
        if not fok:
            out["state"] = "offline"
            out["message"] = f"could not reach the remote ({fmsg[:80]})"
            return out

    ok, counts = _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], path)
    if ok and counts:
        parts = counts.split()
        if len(parts) == 2:
            out["ahead"], out["behind"] = int(parts[0]), int(parts[1])
    else:
        out["state"] = "no-upstream"
        out["message"] = "no upstream branch configured"
        return out

    out["update_available"] = out["behind"] > 0
    if out["behind"] and out["dirty"]:
        out["state"] = "behind-dirty"
        out["message"] = (f"{out['behind']} update(s) available, but there are local "
                          "changes - update would not be a fast-forward")
    elif out["behind"]:
        out["state"] = "behind"
        out["message"] = f"{out['behind']} update(s) available"
    elif out["dirty"]:
        out["state"] = "current-dirty"
        out["message"] = "up to date (local changes present)"
    else:
        out["state"] = "current"
        out["message"] = "up to date"
    return out


def check_all(config: Config, fetch: bool = True) -> list[dict[str, Any]]:
    if not git_available():
        return [
            {**status(s, fetch=False), "state": "no-git",
             "message": "git is not installed - version checks unavailable"}
            for s in component_specs(config)
        ]
    return [status(s, fetch=fetch) for s in component_specs(config)]


def update(config: Config, key: str, keep_local: bool = False) -> dict[str, Any]:
    """Fast-forward one component to its remote.

    ``keep_local`` adds ``--autostash``: git shelves uncommitted changes, does the
    fast-forward, then re-applies them. That is what makes a repo with local edits
    updatable at all - without it ``pull --ff-only`` simply refuses, and the user
    is left staring at "2 behind" with no way to act.
    """
    spec = next((s for s in component_specs(config) if s["key"] == key), None)
    if spec is None:
        return {"ok": False, "error": f"unknown component '{key}'"}
    path = Path(spec["path"])
    if not (path / ".git").exists():
        return {"ok": False, "error": "not a git checkout", "status": status(spec, False)}

    args = ["pull", "--ff-only"]
    if keep_local:
        args.insert(1, "--autostash")
    ok, out = _git(args, path, timeout=120)

    result: dict[str, Any] = {"ok": ok, "log": out, "status": status(spec, fetch=False)}
    if not ok:
        low = (out or "").lower()
        if "local changes" in low or "overwritten" in low or "unstaged" in low:
            result["error"] = (
                "Local changes in this folder block the update. Use "
                "'Update (keep my changes)' to shelve and re-apply them."
            )
            result["needs_keep_local"] = True
        elif "diverge" in low or "not possible to fast-forward" in low:
            result["error"] = (
                "This checkout has commits the remote does not, so it cannot be "
                "fast-forwarded. Resolve it in git."
            )
        else:
            result["error"] = out or "git pull failed"
    result["restart_required"] = ok and key == "cd-compliance-checks"
    return result
