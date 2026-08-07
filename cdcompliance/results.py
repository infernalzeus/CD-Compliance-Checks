"""Read and aggregate generated outputs for the dashboard's Panel 2.

Everything here is read-only and metadata-only where OneDrive is involved (it
never hydrates a file). It provides:

- ``source_tree``   : the staging file listing for a participant (Panel 1 bottom,
                      the "WT Onedrive" view), with per-file download state.
- ``output_items``  : the processed items found in the output tree, with their
                      compliance summary.
- ``aggregate``     : combined per-day / per-season / per-device numbers across a
                      set of selected participants (Panel 2 right panel).
- ``item_measures`` : Step 2 reported measures for one participant+season+device
                      (Panel 2 bottom drill-down).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .config import Config
from . import onedrive
from .manifest import cd_suffix, list_cd_participants

_SEASON_TYPES = ("winter", "spring", "summer", "autumn")


def season_type(season_name: str) -> str:
    low = season_name.strip().lower()
    for t in _SEASON_TYPES:
        if low.startswith(t):
            return t.capitalize()
    return "Other"


# ---------------------------------------------------------------------------
# Panel 1 — source ("WT Onedrive") file listing
# ---------------------------------------------------------------------------
def source_tree(config: Config, participant: str, device_filter: Optional[list[str]] = None) -> dict[str, Any]:
    root = config.paths.source_root / participant
    tree: dict[str, Any] = {"participant": participant, "root": str(root), "exists": root.exists(), "seasons": []}
    if not root.exists():
        return tree

    for season_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        season = {"name": season_dir.name, "devices": []}
        for dev_dir in sorted(p for p in season_dir.iterdir() if p.is_dir()):
            files = []
            try:
                for f in sorted(p for p in dev_dir.iterdir() if p.is_file()):
                    try:
                        size = f.stat().st_size
                        dehydrated = onedrive.is_dehydrated(f)
                    except OSError:
                        size, dehydrated = 0, False
                    files.append(
                        {
                            "name": f.name,
                            "size_bytes": size,
                            "size_mb": round(size / (1024 * 1024), 1),
                            "dehydrated": dehydrated,
                        }
                    )
            except OSError:
                pass
            season["devices"].append({"name": dev_dir.name, "files": files})
        # loose files directly under the season folder (assessment logs etc.)
        loose = []
        for f in sorted(p for p in season_dir.iterdir() if p.is_file()):
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            loose.append({"name": f.name, "size_mb": round(size / (1024 * 1024), 1)})
        season["files"] = loose
        tree["seasons"].append(season)
    return tree


# ---------------------------------------------------------------------------
# Output reading
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def output_items(config: Config, participant: str) -> list[dict[str, Any]]:
    """All processed items for a participant found under the output tree."""
    root = config.paths.output_root / participant
    items: list[dict[str, Any]] = []
    if not root.exists():
        return items

    for season_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for dev_dir in sorted(p for p in season_dir.iterdir() if p.is_dir()):
            for comp_json in sorted(dev_dir.glob("*_compliance.json")):
                data = _read_json(comp_json)
                if not data:
                    continue
                comp = data.get("compliance", {})
                summary = comp.get("summary", {})
                stem = comp_json.name[: -len("_compliance.json")]
                items.append(
                    {
                        "participant": participant,
                        "season": season_dir.name,
                        "season_type": season_type(season_dir.name),
                        "device": dev_dir.name.lower(),
                        "stem": stem,
                        "verdict": comp.get("verdict"),
                        "pct_compliance_mean": summary.get("pct_compliance_mean"),
                        "valid_days": summary.get("valid_days"),
                        "total_days": summary.get("total_days"),
                        "output_dir": str(dev_dir),
                    }
                )
    return items


def panel2_grid(config: Config) -> list[dict[str, Any]]:
    """Grid model for Panel 2, coloured by compliance of the processed items.

        green  : every processed recording in the folder is compliant (PASS)
        red    : none are compliant
        orange : some are compliant
        empty  : folder present but nothing processed yet
    """
    return [panel2_cell(config, name) for name in list_cd_participants(config.paths.output_root)]


def panel2_cell(config: Config, name: str) -> dict[str, Any]:
    """One Panel-2 grid cell (compliance-coloured) for a participant."""
    items = output_items(config, name)
    total = len(items)
    npass = sum(1 for it in items if (it.get("verdict") or "").upper() == "PASS")
    if total == 0:
        state = "empty"
    elif npass == total:
        state = "green"
    elif npass == 0:
        state = "red"
    else:
        state = "orange"
    return {
        "participant": name,
        "suffix": cd_suffix(name) or name,
        "state": state,
        "done": npass,       # compliant count
        "total": total,      # processed recordings
    }


def aggregate(config: Config, participants: list[str]) -> dict[str, Any]:
    """Combined numbers across selected participants (Panel 2 right panel)."""
    all_items: list[dict[str, Any]] = []
    for p in participants:
        all_items.extend(output_items(config, p))

    season_dataset_keys = {(it["participant"], it["season"]) for it in all_items}
    seasons_by_type: dict[str, int] = {}
    for _p, s in season_dataset_keys:
        st = season_type(s)
        seasons_by_type[st] = seasons_by_type.get(st, 0) + 1

    # Per-device rollup.
    devices: dict[str, Any] = {}
    for it in all_items:
        dev = it["device"]
        d = devices.setdefault(
            dev,
            {"n_items": 0, "passed": 0, "review": 0, "failed": 0, "pct_values": [], "per_season": {}},
        )
        d["n_items"] += 1
        verdict = (it.get("verdict") or "").upper()
        if verdict == "PASS":
            d["passed"] += 1
        elif verdict == "REVIEW":
            d["review"] += 1
        elif verdict in ("FAIL", "ERROR"):
            d["failed"] += 1
        pct = it.get("pct_compliance_mean")
        if isinstance(pct, (int, float)):
            d["pct_values"].append(float(pct))
        st = it["season_type"]
        ps = d["per_season"].setdefault(st, {"n": 0, "passed": 0, "pct_values": []})
        ps["n"] += 1
        if verdict == "PASS":
            ps["passed"] += 1
        if isinstance(pct, (int, float)):
            ps["pct_values"].append(float(pct))

    # Reduce pct lists to means.
    for d in devices.values():
        vals = d.pop("pct_values")
        d["pct_compliance_mean"] = round(sum(vals) / len(vals), 2) if vals else None
        for ps in d["per_season"].values():
            pv = ps.pop("pct_values")
            ps["pct_compliance_mean"] = round(sum(pv) / len(pv), 2) if pv else None

    return {
        "participants": participants,
        "n_participants": len(participants),
        "n_season_datasets": len(season_dataset_keys),
        "seasons_by_type": seasons_by_type,
        "devices": devices,
        "items": all_items,
    }


# ---------------------------------------------------------------------------
# Panel 2 bottom — Step 2 measures for one item
# ---------------------------------------------------------------------------
def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        import pandas as pd

        df = pd.read_csv(path)
        return df.to_dict(orient="records")
    except Exception:
        return []


def item_measures(
    config: Config, participant: str, season: str, device: str, stem: str
) -> dict[str, Any]:
    """Compliance summary + the list of inspectable output files for one item.

    Step 2 / daily files are named after the epoch stem (``<stem>_60s_...``); the
    ``outputs`` list gives each available CSV / PDF so the UI can link to it.
    """
    dev_dir = config.paths.output_root / participant / season / _device_folder(config, device)
    epoch_stem = f"{stem}_60s"
    out: dict[str, Any] = {
        "participant": participant,
        "season": season,
        "device": device,
        "stem": stem,
        "output_dir": str(dev_dir),
    }

    comp = _read_json(dev_dir / f"{stem}_compliance.json")
    out["compliance"] = comp.get("compliance") if comp else None

    outputs: list[dict[str, Any]] = []
    if device.lower() == "mieye":
        # Luminosity outputs are named after the light-CSV stem directly.
        csv_specs = [
            ("Per-day light metrics", f"{stem}_luminosity_metrics.csv"),
            ("Daily compliance", f"{stem}_daily_compliance.csv"),
        ]
    else:
        csv_specs = [
            ("Non-parametric (IS · IV · M10/L5)", f"{epoch_stem}_nonparametric.csv"),
            ("Per-day M10 / L5", f"{epoch_stem}_daily.csv"),
            ("Periodogram (14–34 h)", f"{epoch_stem}_periodogram.csv"),
            ("SRI (sleep regularity)", f"{epoch_stem}_sri.csv"),
            ("Daily compliance", f"{epoch_stem}_daily_compliance.csv"),
        ]
    for label, name in csv_specs:
        if (dev_dir / name).exists():
            outputs.append({"label": label, "kind": "csv", "name": name})
    for pdf in sorted(dev_dir.glob(f"{stem}*.pdf")):
        outputs.append({"label": pdf.name, "kind": "pdf", "name": pdf.name})
    out["outputs"] = outputs
    return out


def _device_folder(config: Config, device: str) -> str:
    key = device.lower()
    if key == "actigraph":
        return config.actigraph_folder_name
    if key == "mieye":
        return config.mieye_folder_name
    # Fall back to the registered processor's folder name (correct casing), then
    # to a capitalised guess for unknown devices.
    try:
        from .devices.registry import get_processor

        folder = get_processor(key).folder_name
        if folder:
            return folder
    except Exception:
        pass
    return device.capitalize()
