"""Naming checks for input files, run before processing.

The pipeline finds its inputs by folder and filename. When an upload drifts from
the study's naming convention, a sample can be silently skipped (an Expiwell
export never renamed with its participant prefix, a MiEYE file still called
``-download.csv``) or processed under the wrong identity (a recording whose
filename ID doesn't match its folder). This module surfaces those cases *before*
a run, so they can be fixed or at least seen.

Scope is deliberately narrow: only files the pipeline actually processes are
checked, mirroring each device's discovery rules. The many other files uploaded
alongside them (assessment logs, consent PDFs, configuration screenshots) are
ignored, as are folders with nothing to process.

Patterns, expected formats, examples, messages and severities live in
``naming_rules.yaml`` so a convention change is an edit there, not here.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import Config

RULES_PATH = Path(__file__).with_name("naming_rules.yaml")
SEVERITIES = ("skipped", "warning", "info")

_ID_RE = re.compile(r"CD\d+", re.IGNORECASE)
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}")
_DEVICE_RE = re.compile(r"(?<![A-Za-z0-9])M\d+(?![A-Za-z0-9])")
_SEASON_TAG_RE = re.compile(r"(?<![A-Za-z0-9])\(?S\d\)?(?![A-Za-z0-9])")


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    with RULES_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _files(folder: Path) -> list[str]:
    """Plain filenames in *folder* (no recursion, no OneDrive download)."""
    try:
        with os.scandir(folder) as it:
            return sorted(e.name for e in it if e.is_file() and e.name.lower() != "desktop.ini")
    except OSError:
        return []


def _subdirs(folder: Path) -> list[str]:
    try:
        with os.scandir(folder) as it:
            return sorted(e.name for e in it if e.is_dir())
    except OSError:
        return []


class _Collector:
    """Accumulates flags with the shared context filled in."""

    def __init__(self, participant: str) -> None:
        self.participant = participant
        self.rules = load_rules()
        self.flags: list[dict[str, Any]] = []

    def add(self, rule_id: str, season: str, device: Optional[str], file: Optional[str],
            **fmt: Any) -> None:
        rule = self.rules.get("rules", {}).get(rule_id, {})
        dev_rules = self.rules.get("devices", {}).get(device or "", {})
        if device is None:
            dev_rules = self.rules.get("season_folder", {})
        message = rule.get("message", rule_id)
        try:
            message = message.format(**fmt)
        except (KeyError, IndexError):
            pass
        self.flags.append({
            "severity": rule.get("severity", "info"),
            "rule": rule_id,
            "participant": self.participant,
            "season": season,
            "device": device,
            "file": file,
            "message": message,
            "expected": dev_rules.get("expected"),
            "example": dev_rules.get("example"),
        })


def _check_id(c: _Collector, name: str, season: str, device: str) -> None:
    m = _ID_RE.search(name)
    if not m:
        c.add("id-missing", season, device, name)
    elif m.group(0).upper() != c.participant.upper():
        c.add("id-mismatch", season, device, name,
              found=m.group(0).upper(), folder=c.participant)


def _check_actigraph(c: _Collector, season: str, folder: Path) -> bool:
    spec = c.rules["devices"]["actigraph"]
    names = _files(folder)
    bins = [n for n in names if n.lower().endswith(".bin")]
    if not bins:
        raw = [n for n in names if n.lower().endswith(".csv")
               and "_60s" not in n.lower() and _TS_RE.search(n)]
        for n in raw:
            c.add("actigraph-no-bin", season, "actigraph", n)
        return False
    pattern = re.compile(spec["pattern"])
    for n in bins:
        # Only a leading ID identifies the recording; anything later is noise.
        lead = re.match(r"(CD\d+)", n, re.IGNORECASE)
        if not lead:
            c.add("id-missing", season, "actigraph", n)
        elif lead.group(1).upper() != c.participant.upper():
            c.add("id-mismatch", season, "actigraph", n,
                  found=lead.group(1).upper(), folder=c.participant)
        if not pattern.match(n):
            c.add("actigraph-off-pattern", season, "actigraph", n)
    if len(bins) > 1:
        c.add("actigraph-multiple", season, "actigraph", None, count=len(bins))
    return True


def _check_mieye(c: _Collector, config: Config, season: str, folder: Path) -> bool:
    spec = c.rules["devices"]["mieye"]
    names = _files(folder)
    logged = [n for n in names if n.lower().endswith("-logged.csv")]
    download = [n for n in names if n.lower().endswith("-download.csv")]
    # Mirror MiEyeProcessor.discover: -logged.csv first, then -download.csv.
    candidates = logged or download
    other_light = [n for n in names if n.lower().endswith((".csv", ".xlsx"))
                   and n not in logged and n not in download]
    if not candidates:
        for n in other_light:
            c.add("mieye-unreadable", season, "mieye", n)
        return False

    # Mirror MiEyeProcessor.discover: a file carrying this participant's ID wins.
    own = [n for n in candidates if c.participant.upper() in n.upper()]
    candidates = own + [n for n in candidates if n not in own]
    used = candidates[0]
    for extra in (candidates[1:] + ([] if not logged else download)):
        c.add("mieye-extra-files", season, "mieye", extra, used=used)
    if not logged:
        c.add("mieye-download-name", season, "mieye", used)

    _check_id(c, used, season, "mieye")
    if not re.match(spec["pattern"], used):
        # Name the specific missing parts where we can; otherwise it's order/format.
        specific = False
        if not _SEASON_TAG_RE.search(used):
            c.add("mieye-no-season-tag", season, "mieye", used); specific = True
        if not _DEVICE_RE.search(used):
            c.add("mieye-no-device", season, "mieye", used); specific = True
        if not specific and not used.lower().endswith("-download.csv"):
            c.add("mieye-off-pattern", season, "mieye", used)
    return True


def _check_expiwell(c: _Collector, config: Config, season: str, folder: Path) -> bool:
    spec = c.rules["devices"]["expiwell"]
    names = [n for n in _files(folder) if n.lower().endswith(".csv")]
    if not names:
        return False
    named = [n for n in names if "expiwell" in n.lower()]
    if not named:
        # Mirror ExpiwellProcessor._survey_files: fall back to every CSV here.
        for n in names:
            c.add("expiwell-not-renamed", season, "expiwell", n)
        return True
    pattern = re.compile(spec["pattern"])
    for n in named:
        _check_id(c, n, season, "expiwell")
        if not pattern.match(n):
            c.add("expiwell-off-pattern", season, "expiwell", n)
    return True


def check_participant(config: Config, participant: str) -> list[dict[str, Any]]:
    """Every naming flag for one participant's input folders."""
    c = _Collector(participant)
    rules = c.rules
    root = Path(config.paths.source_root) / participant
    if not root.is_dir():
        return []
    season_re = re.compile(rules["season_folder"]["pattern"], re.IGNORECASE)
    folder_names = {
        "actigraph": config.actigraph_folder_name,
        "mieye": config.mieye_folder_name,
        "expiwell": config.expiwell_folder_name,
    }
    for season in _subdirs(root):
        sdir = root / season
        processed = False
        processed |= _check_actigraph(c, season, sdir / folder_names["actigraph"])
        processed |= _check_mieye(c, config, season, sdir / folder_names["mieye"])
        processed |= _check_expiwell(c, config, season, sdir / folder_names["expiwell"])
        if not processed:
            continue           # nothing processed in this season: nothing to flag
        tidy = re.sub(r"\s+", " ", season.strip())
        if not season_re.match(tidy):
            c.add("season-unrecognised", season, None, None)
        elif tidy != season:
            c.add("season-spacing", season, None, None)
    return c.flags


def summarise(flags: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s: 0 for s in SEVERITIES}
    for f in flags:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Expected-vs-current view for one participant
# ---------------------------------------------------------------------------
_DEVICE_ORDER = ("actigraph", "mieye", "expiwell")


def _used_files(config: Config, device: str, folder: Path) -> list[str]:
    """Files the pipeline takes from *folder* - mirrors each device's discovery."""
    names = _files(folder)
    if device == "actigraph":
        return [n for n in names if n.lower().endswith(".bin")]
    if device == "mieye":
        logged = [n for n in names if n.lower().endswith("-logged.csv")]
        return logged or [n for n in names if n.lower().endswith("-download.csv")]
    if device == "expiwell":
        csvs = [n for n in names if n.lower().endswith(".csv")]
        named = [n for n in csvs if "expiwell" in n.lower()]
        return named or csvs
    return []


def describe_participant(config: Config, participant: str) -> dict[str, Any]:
    """Each season/device folder: the naming convention beside the files there.

    For every file the pipeline uses (plus any it cannot use but should), give
    its current name, a status - ok / info / warning / skipped - and the reasons.
    This is what lets someone compare "what it should be called" with "what it
    is called" at a glance, rather than reading a list of problems only.
    """
    rules = load_rules()
    flags = check_participant(config, participant)
    root = Path(config.paths.source_root) / participant
    rank = {"ok": 0, "info": 1, "warning": 2, "skipped": 3}

    by_file: dict[tuple, list[dict[str, Any]]] = {}
    device_notes: dict[tuple, list[dict[str, Any]]] = {}
    season_notes: dict[str, list[dict[str, Any]]] = {}
    for f in flags:
        if f["device"] is None:
            season_notes.setdefault(f["season"], []).append(f)
        elif f["file"] is None:
            device_notes.setdefault((f["season"], f["device"]), []).append(f)
        else:
            by_file.setdefault((f["season"], f["device"], f["file"]), []).append(f)

    folder_names = {
        "actigraph": config.actigraph_folder_name,
        "mieye": config.mieye_folder_name,
        "expiwell": config.expiwell_folder_name,
    }
    season_spec = rules.get("season_folder", {})
    seasons: list[dict[str, Any]] = []
    for season in (_subdirs(root) if root.is_dir() else []):
        devices = []
        for dev in _DEVICE_ORDER:
            spec = rules.get("devices", {}).get(dev, {})
            names = list(_used_files(config, dev, root / season / folder_names[dev]))
            # Files the pipeline can't use but that were flagged still belong here.
            for (s, d, fname) in by_file:
                if s == season and d == dev and fname not in names:
                    names.append(fname)
            notes = device_notes.get((season, dev), [])
            if not names and not notes:
                continue
            files = []
            for n in names:
                fl = by_file.get((season, dev, n), [])
                status = max((x["severity"] for x in fl), key=lambda v: rank[v], default="ok")
                files.append({"name": n, "status": status,
                              "reasons": [{"severity": x["severity"], "message": x["message"]} for x in fl]})
            files.sort(key=lambda x: (-rank[x["status"]], x["name"]))
            devices.append({
                "device": dev, "folder": folder_names[dev],
                "expected": spec.get("expected"), "example": spec.get("example"),
                "status": max([x["status"] for x in files] + [n["severity"] for n in notes],
                              key=lambda v: rank[v], default="ok"),
                "files": files,
                "notes": [{"severity": n["severity"], "message": n["message"]} for n in notes],
            })
        if not devices:
            continue
        snotes = season_notes.get(season, [])
        seasons.append({
            "name": season,
            "expected": season_spec.get("expected"), "example": season_spec.get("example"),
            "status": max([n["severity"] for n in snotes], key=lambda v: rank[v], default="ok"),
            "notes": [{"severity": n["severity"], "message": n["message"]} for n in snotes],
            "devices": devices,
        })
    return {"participant": participant, "exists": root.is_dir(),
            "counts": summarise(flags), "seasons": seasons}
