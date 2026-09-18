"""T2 phase P5: the one step that writes a dataset.

Everything else in T2 reads. Export takes the preview exactly as it stands -
the same day range, cut-offs and measures - and writes it to the per-user
``t2_root`` with enough provenance to reproduce it:

    <t2_root>/<selection-id>_<when>/
        data_long.csv        one row per participant / date / measure
        data_wide_day.csv    one row per participant-day, one column per measure
        season_summary.csv   one row per participant-season / measure
        dictionary.csv       the measures used, with units and definitions
        manifest.json        who, when, what was included, and the exact inputs
        README.txt           what the files are, in plain words

The files are written to a temporary folder beside the destination and renamed
into place, so a half-written export never appears under ``t2_root`` (which may
be a shared drive). Exports are never edited: exporting again makes a new one.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import batches, pseudo_id, t2_data, t2_report
from .config import Config

#: What each file is for, in the order they are listed and written.
FILES = [
    ("data_long.csv", "One row per participant, date and measure - the tidy table for analysis."),
    ("data_wide_day.csv", "One row per participant-day, one column per measure - for a quick look in Excel."),
    ("season_summary.csv", "One row per participant-season and measure: days, mean, sd, range."),
    ("dictionary.csv", "Every measure in this export: device, unit, definition and the file it came from."),
    ("manifest.json", "Provenance: who exported it, when, what was included and the exact input files."),
    ("README.txt", "What each file contains."),
    ("report.pdf", "The charts, as previewed in T2: coverage, each measure by season, day-by-day timelines."),
    ("reports/", "The device report PDFs for the participant-seasons included here."),
]

README = """T2 export - CHiP-D compliance dashboard
=======================================

This folder is a snapshot. It was written once and is never edited: exporting
again creates a new folder.

{files}

How to read the data
--------------------
* Every row is a day that had already passed the compliance (Visualise) stage.
* "date" is the real calendar date. "night_of" is the evening a night started,
  so a sleep diary filled in on the morning of the 5th has night_of = the 4th.
* "window_day" counts days inside the window where all the selected devices
  were recording at once (day 1 = the first such day).
* "part_day" marks a first or last day that was only partly recorded.
* Measures come from different devices and are on different scales - see
  dictionary.csv for the unit of each one.
* "valid" is the device's own valid-day flag from the compliance stage, kept
  for reference only. T2 does not filter on it: every day here had already
  passed compliance before it reached T2.

What was included
-----------------
{inclusion}
"""


def _display_ids(config: Config, participants: list[str]) -> dict[str, str]:
    return pseudo_id.mapping(config, participants)


def _long_table(frame, ids: dict[str, str]) -> pd.DataFrame:
    long = frame.long.copy()
    if long.empty:
        return long
    long["participant"] = long["participant"].map(lambda p: ids.get(p, p))
    long = long.drop(columns=["display_id", "recording"], errors="ignore")
    long = long.rename(columns={"participant": "id", "partial": "part_day",
                                "season": "season_folder", "season_label": "season"})
    keep = ["id", "season", "season_folder", "season_n", "season_key", "device", "variable",
            "level", "date", "night_of", "day_of_study", "value", "part_day", "in_window",
            "window_day", "valid"]
    return long[[c for c in keep if c in long.columns]]


def _wide_day(long: pd.DataFrame) -> pd.DataFrame:
    d = long[long["level"] == "day"] if not long.empty else long
    if d.empty:
        return pd.DataFrame()
    return (d.pivot_table(index=["id", "season", "season_n", "date", "window_day"],
                          columns="variable", values="value", aggfunc="mean")
             .reset_index())


def _season_summary(long: pd.DataFrame) -> pd.DataFrame:
    if long.empty:
        return pd.DataFrame()
    d = long[long["level"] == "day"]
    if d.empty:
        return pd.DataFrame()
    g = d.groupby(["id", "season", "season_n", "device", "variable"])["value"]
    out = g.agg(["count", "mean", "std", "min", "max"]).reset_index()
    return out.rename(columns={"count": "n_days", "std": "sd"}).round(3)


def _dictionary_rows(variables: list[str]) -> list[dict[str, Any]]:
    dictionary = t2_data.load_dictionary()
    sources = dictionary["sources"]
    rows = []
    for v in dictionary["variables"]:
        if v["id"] not in variables and not any(x.startswith(v["id"] + "_") for x in variables):
            continue
        spec = sources.get(v["source"], {})
        rows.append({
            "measure": v["id"], "device": v["device"], "level": v["level"],
            "unit": v.get("unit", ""), "definition": v.get("definition", ""),
            "timing": v.get("timing", "day"),
            "source_file": spec.get("glob", ""), "source_column": v["column"],
        })
    return rows


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def included_seasons(long: pd.DataFrame) -> set[tuple[str, str]]:
    """(participant, season folder) pairs that actually made it into the export."""
    if long.empty or "season_folder" not in long.columns:
        return set()
    return {(pid, t2_data._norm_season(str(folder)))
            for pid, folder in long[["id", "season_folder"]].drop_duplicates().to_numpy()}


def device_reports(config: Config, sel: t2_data.T2Selection,
                   included: Optional[set] = None) -> list[dict[str, Any]]:
    """The report PDFs the device tools produced, for the seasons being exported.

    Only the participant-seasons in the export are copied - a Winter report has
    no business in an Autumn export. Their filenames carry the real participant
    ID, so they are left out entirely while pseudonymous IDs are switched on: a
    PDF named after the participant would undo the pseudonymisation.
    """
    if pseudo_id.enabled(config):
        return []
    from .results import _device_folder

    out: list[dict[str, Any]] = []
    for pid in sel.participants:
        root = config.paths.output_root / pid
        if not root.is_dir():
            continue
        for season_dir in (p for p in root.iterdir() if p.is_dir()):
            # Folder names are matched loosely: one real folder is "Autumn  2025"
            # with two spaces, while the table holds the tidied name.
            if included is not None and (pid, t2_data._norm_season(season_dir.name)) not in included:
                continue
            for device in sel.devices:
                dev_dir = season_dir / _device_folder(config, device)
                if not dev_dir.is_dir():
                    continue
                for f in sorted(dev_dir.glob("*.pdf")):
                    out.append({"participant": pid, "season": season_dir.name, "device": device,
                                "path": f, "name": f.name,
                                "bytes": f.stat().st_size if f.exists() else 0})
    return out


def source_files(config: Config, sel: t2_data.T2Selection) -> list[dict[str, Any]]:
    """Every output file the preview read, hashed, so the export is traceable."""
    dictionary = t2_data.load_dictionary()
    from .results import _device_folder

    seen: dict[Path, dict[str, Any]] = {}
    for pid in sel.participants:
        root = config.paths.output_root / pid
        if not root.is_dir():
            continue
        for season_dir in (p for p in root.iterdir() if p.is_dir()):
            for spec in dictionary["sources"].values():
                if spec["device"] not in sel.devices:
                    continue
                dev_dir = season_dir / _device_folder(config, spec["device"])
                for f in sorted(dev_dir.glob(spec["glob"])):
                    if f in seen:
                        continue
                    try:
                        stat = f.stat()
                        seen[f] = {
                            "file": str(f.relative_to(config.paths.output_root)),
                            "bytes": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                            "sha256": _sha256(f),
                        }
                    except OSError:
                        continue
    return list(seen.values())


def build(config: Config, batch_id: str, sel: t2_data.T2Selection, frame,
          options: dict[str, Any]) -> dict[str, Any]:
    """The tables and the numbers behind them - shared by preview and export."""
    ids = _display_ids(config, sel.participants)
    long = _long_table(frame, ids)
    if options.get("day_level_only", False) and not long.empty:
        long = long[long["level"] == "day"]
    wide = _wide_day(long)
    seasons = _season_summary(long)
    measures = sorted(long["variable"].unique().tolist()) if not long.empty else []
    tables = {
        "data_long.csv": long,
        "data_wide_day.csv": wide,
        "season_summary.csv": seasons,
    }
    inclusion = {
        "selection": batch_id,
        "participants": len(sel.participants),
        "participant_seasons": int(long[["id", "season"]].drop_duplicates().shape[0]) if not long.empty else 0,
        "seasons": sorted(long["season"].dropna().unique().tolist()) if not long.empty else [],
        "devices": sel.devices,
        "measures": len(measures),
        "days": int(long[long["level"] == "day"][["id", "season", "date"]].drop_duplicates().shape[0])
                if not long.empty else 0,
        "day_range": options.get("days"),
        "part_days": "dropped" if options.get("exclude_partial", True) else "included",
        "cut_offs": options.get("cutoffs") or [],
    }
    return {"tables": tables, "dictionary": _dictionary_rows(measures), "inclusion": inclusion,
            "measures": measures}


def preview(config: Config, batch_id: str, sel: t2_data.T2Selection, frame,
            options: dict[str, Any]) -> dict[str, Any]:
    """What would be written, before anything is written."""
    built = build(config, batch_id, sel, frame, options)
    reports = device_reports(config, sel, included_seasons(built["tables"]["data_long.csv"]))
    files = []
    for name, what in FILES:
        entry = {"name": name, "what": what}
        if name == "reports/":
            entry.update({"rows": len(reports), "columns": None,
                          "bytes": sum(r["bytes"] for r in reports)})
            if not reports and pseudo_id.enabled(config):
                entry["what"] = "skipped: report filenames contain the real participant ID"
            files.append(entry)
            continue
        table = built["tables"].get(name)
        if table is not None:
            entry.update({"rows": int(len(table)), "columns": int(len(table.columns))})
            entry["bytes"] = int(table.memory_usage(deep=True).sum()) if len(table) else 0
        elif name == "dictionary.csv":
            entry.update({"rows": len(built["dictionary"]), "columns": 8})
        elif name == "report.pdf" and not t2_report.available():
            entry["what"] = "skipped: matplotlib is not installed in this environment"
        files.append(entry)
    destination = Path(config.paths.t2_root) / f"{batch_id}_<time>"
    return {
        "files": files,
        "inclusion": built["inclusion"],
        "destination": str(destination),
        "t2_root": str(config.paths.t2_root),
        "t2_root_exists": Path(config.paths.t2_root).exists(),
        "empty": built["inclusion"]["days"] == 0,
    }


def write(config: Config, batch_id: str, sel: t2_data.T2Selection, frame,
          options: dict[str, Any], approver: str, name: str = "") -> dict[str, Any]:
    """Write the export, then record it. Raises ValueError on anything unsafe."""
    initials = batches.normalise_initials(approver)
    if initials is None:
        raise ValueError("the approver's initials (2-4 letters) are required")
    built = build(config, batch_id, sel, frame, options)
    if built["inclusion"]["days"] == 0:
        raise ValueError("nothing to export: no days in the current range")

    when = datetime.now()
    # The selection id already carries the date it was sent; repeating it here
    # gave folders two timestamps. The export is named for when IT happened,
    # in the same shape as a batch id, and the manifest links back.
    tail = batch_id.split("-", 3)[-1] if batch_id.count("-") >= 3 else batch_id
    folder_name = f"T2-{when:%Y%m%d-%H%M%S}-{tail}"
    root = Path(config.paths.t2_root)
    # Two exports can land in the same second (a re-export, a double-click), so
    # give the second one its own folder rather than refusing it.
    base, suffix = folder_name, 2
    while (root / folder_name).exists():
        folder_name = f"{base}-{suffix}"
        suffix += 1
    destination = root / folder_name
    staging = root / f".{folder_name}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        for filename, table in built["tables"].items():
            table.to_csv(staging / filename, index=False)
        with (staging / "dictionary.csv").open("w", newline="", encoding="utf-8") as fh:
            fields = ["measure", "device", "level", "unit", "definition", "timing",
                      "source_file", "source_column"]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(built["dictionary"])

        reports = device_reports(config, sel, included_seasons(built["tables"]["data_long.csv"]))
        for r in reports:
            dest = staging / "reports" / r["participant"] / r["season"]
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(r["path"], dest / r["name"])

        manifest = {
            "export_id": folder_name,
            "selection_id": batch_id,
            "name": name,
            "approved_by": initials,
            "exported_at": when.isoformat(timespec="seconds"),
            "preview_only_until_now": True,
            "inclusion": built["inclusion"],
            "pseudonymous_ids": pseudo_id.status(config),
            "measures": built["measures"],
            "thresholds_in_force": batches._thresholds(config),
            "tool_versions": batches._tool_versions(config),
            "source_files": source_files(config, sel),
            "device_reports": [{k: v for k, v in r.items() if k != "path"} for r in reports],
            "notes": [
                "Every day here had already passed the compliance stage; T2 does not re-judge it.",
                "Analyses previewed in T2 are exploratory.",
            ],
        }
        charts = t2_report.build_pdf(staging / "report.pdf", built["tables"]["data_long.csv"],
                                     built["dictionary"], manifest, built["inclusion"])
        manifest["charts_pdf"] = charts or "skipped (matplotlib not installed)"
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                               encoding="utf-8")
        lines = "\n".join(f"* {n} - {w}" for n, w in FILES)
        inclusion_lines = "\n".join(f"* {k.replace('_', ' ')}: {v}"
                                    for k, v in built["inclusion"].items())
        (staging / "README.txt").write_text(
            README.format(files=lines, inclusion=inclusion_lines), encoding="utf-8")
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    written = []
    for f in sorted(destination.iterdir()):
        if f.is_dir():
            inner = [x for x in f.rglob("*") if x.is_file()]
            written.append({"name": f"{f.name}/ ({len(inner)} files)",
                            "bytes": sum(x.stat().st_size for x in inner)})
        else:
            written.append({"name": f.name, "bytes": f.stat().st_size})
    return {"export_id": folder_name, "destination": str(destination), "files": written,
            "inclusion": built["inclusion"], "approved_by": initials, "manifest": manifest}
