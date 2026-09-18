"""Per-user dashboard settings (folders chosen in the ⚙ panel).

Each person can point the dashboard at different input, output and T2 folders,
and at their own copies of the tool repositories. Those choices are stored in the
**user's own profile**, not in the app folder:

    Windows  %APPDATA%\\CHiP-D Dashboard\\settings.json
    macOS    ~/Library/Application Support/CHiP-D Dashboard/settings.json
    Linux    ~/.config/CHiP-D Dashboard/settings.json

That matters when one installation is shared (for example on a network drive):
settings saved in the app folder would be shared too, so one person's change
would silently repoint everyone else. Storing them per user keeps each person's
choices on their own account, and they survive updates because the update never
touches the profile folder.

``CDCC_SETTINGS`` overrides the location (used by tests). A settings file left in
the app folder by older versions is copied into the profile the first time, so
nobody loses their folders on upgrade.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Config

APP_DIR_NAME = "CHiP-D Dashboard"

#: settings key -> (config section, attribute)
FIELDS: dict[str, tuple[str, str]] = {
    "source_root": ("paths", "source_root"),
    "output_root": ("paths", "output_root"),
    "t2_root": ("paths", "t2_root"),
    "epoching_repo": ("tools", "epoching_repo"),
    "sleep_metrics_repo": ("tools", "sleep_metrics_repo"),
    "luminosity_repo": ("tools", "luminosity_repo"),
    "expiwell_repo": ("tools", "expiwell_repo"),
}


#: Plain (non-path) per-user values, e.g. the initials recorded on each batch.
TEXT_FIELDS = ("initials",)
_text: dict[str, str] = {}


def get_text(key: str) -> str:
    return _text.get(key, "")


def set_text(key: str, value: str) -> None:
    if key in TEXT_FIELDS:
        _text[key] = value


def settings_path() -> Path:
    override = os.environ.get("CDCC_SETTINGS")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_DIR_NAME / "settings.json"


def _set(config: Config, key: str, value: str) -> None:
    section, attr = FIELDS[key]
    setattr(getattr(config, section), attr, Path(value))


def apply(config: Config, legacy_file: Path | None = None) -> Path:
    """Load this user's settings onto *config*. Returns the settings file path."""
    path = settings_path()
    # Migrate the old app-folder file ONLY into the default per-user location.
    # An explicit CDCC_SETTINGS (tests, a deliberately separate profile) must
    # start clean: copying the legacy file there silently re-points it at the
    # real data folders.
    explicit = bool(os.environ.get("CDCC_SETTINGS"))
    if (not explicit and not path.exists() and legacy_file is not None
            and Path(legacy_file).exists()):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(legacy_file, path)
            print(f"[settings] moved your folder settings to {path}")
        except OSError as exc:
            print(f"[settings] could not migrate {legacy_file}: {exc}")
            path = Path(legacy_file)          # still honour them this session
    if not path.exists():
        return path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # a bad settings file must never stop startup
        print(f"[settings] could not read {path}: {exc}")
        return path
    for key in FIELDS:
        if data.get(key):
            _set(config, key, data[key])
    for key in TEXT_FIELDS:
        if data.get(key):
            _text[key] = str(data[key])
    return path


def update(config: Config, values: dict[str, Any]) -> None:
    """Apply non-empty submitted values (quotes/whitespace stripped)."""
    for key in FIELDS:
        raw = values.get(key)
        if raw is None:
            continue
        cleaned = str(raw).strip().strip('"')
        if cleaned:
            _set(config, key, cleaned)


#: Why the last save failed (shown in the UI - a silent failure means someone
#: keeps using folders they think they changed).
last_error: str = ""


def saved_at() -> str:
    """When the settings file was last written, ISO minutes, or ''."""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(settings_path().stat().st_mtime).isoformat(timespec="minutes")
    except OSError:
        return ""


def save(config: Config) -> bool:
    global last_error
    path = settings_path()
    data = {key: str(getattr(getattr(config, s), a)) for key, (s, a) in FIELDS.items()}
    data.update({k: v for k, v in _text.items() if v})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        last_error = ""
        return True
    except OSError as exc:
        last_error = str(exc)
        print(f"[settings] could not save {path}: {exc}")
        return False
