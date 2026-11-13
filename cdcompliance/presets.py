"""Topic presets — a saved T2 selection, so a dataset can be rebuilt the same way.

A standing-dataset topic is a thirty-variable selection across four devices.
Reassembling it by hand each time is where repeatability goes: two exports of
"the same" topic end up different, and nothing records which one a result came
from.

A preset holds **devices + variables + a name, and nothing else**. Participants
are chosen fresh every time and recorded on the export; seasons belong to a
participant, so they cannot live in a shared preset either.

Every preset is validated against the live data dictionary on load: a variable
id that no longer exists is reported rather than silently dropped, because a
preset that quietly shrinks is worse than one that complains.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import t2_data
from .devices import all_devices

PRESETS_FILE = Path(__file__).resolve().parent / "topic_presets.yaml"


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    path = path or PRESETS_FILE
    if not path.exists():
        return {"version": 1, "presets": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping at the top level")
    return data


def load(path: Path | None = None) -> list[dict[str, Any]]:
    """Every preset, with its variables checked against the dictionary.

    Each returned preset carries:

    ``variables``   the ids that exist and belong to one of its devices
    ``unknown``     ids in the file that the dictionary does not have
    ``n_available`` how many variables the selection actually resolves to
    """
    known_devices = set(all_devices())
    by_device: dict[str, set[str]] = {}
    for v in t2_data.variables():
        by_device.setdefault(v["device"], set()).add(v["id"])

    out: list[dict[str, Any]] = []
    for raw in _load_raw(path).get("presets") or []:
        devices = [str(d).lower() for d in (raw.get("devices") or [])]
        bad_devices = [d for d in devices if d not in known_devices]
        allowed: set[str] = set()
        for d in devices:
            allowed |= by_device.get(d, set())

        wanted = [str(x) for x in (raw.get("variables") or [])]
        if not wanted:
            # An empty list means "all variables for these devices" - it must
            # stay empty in the file so it follows the dictionary as it grows.
            variables, unknown = sorted(allowed), []
        else:
            variables = [v for v in wanted if v in allowed]
            unknown = [v for v in wanted if v not in allowed]

        out.append({
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or raw.get("id") or "untitled"),
            "question": " ".join(str(raw.get("question") or "").split()),
            "devices": devices,
            "unknown_devices": bad_devices,
            "variables": variables,
            "unknown": unknown,
            "all_variables": not wanted,
            "n_available": len(variables),
            "missing": [str(m) for m in (raw.get("missing") or [])],
        })
    return out


def get(preset_id: str, path: Path | None = None) -> dict[str, Any] | None:
    for p in load(path):
        if p["id"] == preset_id:
            return p
    return None
