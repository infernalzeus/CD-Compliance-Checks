"""Recompute: an extra Step 1 epoch file beside a recording, never a replacement.

A *variant* re-runs **Step 1 only**, with different settings, on a recording
that is already in the master tree. Nothing else in the pipeline changes:

- **Step 2 is not run.** A variant is one epoch CSV plus its record. Running
  Step 2 on it is a separate, explicit action.
- **Compliance is never re-evaluated.** The 60-second baseline keeps producing
  the only verdict there is. Several of the compliance thresholds are absolute
  (``min_valid_hours``, ``min_diurnal_range_svm``) and one rule counts epochs
  while its setting is in minutes, so a verdict computed at any other epoch
  length would be quietly wrong. We do not compute one.
- **The baseline is untouched.** Step 1 names its output after the epoch it
  used, so ``<stem>_30s.csv`` can never overwrite ``<stem>_60s.csv``, and
  variants live in their own folder that nothing else globs.

Layout::

    <output>/<CDxxx>/<Season>/Actigraph/
        <stem>_60s.csv                      baseline - untouched
        <stem>_compliance.json              baseline verdict - untouched
        _variants/
            epoch-30s-20261016-143000-YK/
                variant.json                what changed, from what, by whom
                <stem>_30s.csv              the whole output

The raw ``.bin`` is read from the master folder (``copy_bin``), so a recompute
never touches OneDrive. If it is not there the recompute refuses rather than
starting a ~1 GB download nobody asked for.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import tools
from .config import Config
from .events import EventBus

VARIANTS_DIRNAME = "_variants"
DISCARDED_DIRNAME = "_discarded"

#: Only Actigraph has a Step 1 worth varying; the other devices have no epoching
#: stage, so a recompute is meaningless for them.
SUPPORTED_DEVICES = ("actigraph",)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class VariantError(ValueError):
    pass


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def device_dir(config: Config, participant: str, season: str, device: str) -> Path:
    """The master device folder for one participant-season.

    The season is resolved **tolerantly**, not joined literally: season folders
    are named freely on disk (``"Autumn  2025"`` has a double space) while
    ``t2_data.season_numbers`` hands back a whitespace-normalised name. Joining
    the normalised name produces a path that does not exist, which is how a
    participant can silently vanish from a list. ``_output_season_dir`` does
    the same match every other reader uses.
    """
    from .results import _device_folder
    from .t2_data import _output_season_dir

    season_dir = _output_season_dir(config, participant, season)
    if season_dir is None:
        # Keep returning a path (callers check .is_dir()) so a missing season
        # behaves like an empty one rather than raising.
        season_dir = config.paths.output_root / participant / season
    return season_dir / _device_folder(config, device)


def variants_dir(config: Config, participant: str, season: str, device: str) -> Path:
    return device_dir(config, participant, season, device) / VARIANTS_DIRNAME


def make_variant_id(options: dict[str, str], initials: str, when: datetime) -> str:
    """``epoch-30s-20261016-143000-YK`` - the changed setting leads the name."""
    base = _changed_slug(options) or "step1"
    return f"{base}-{when:%Y%m%d-%H%M%S}-{initials}"


def _changed_slug(options: dict[str, str]) -> str:
    defaults = tools.step1_defaults()
    parts = [f"{k}-{str(options[k]).replace('.', 'p')}"
             for k in sorted(options) if str(options[k]) != str(defaults.get(k))]
    return "-".join(parts)[:60]


def changed_from_baseline(options: dict[str, str]) -> dict[str, dict[str, str]]:
    """``{'epoch': {'baseline': '60s', 'variant': '30s'}}`` - only what differs."""
    defaults = tools.step1_defaults()
    return {k: {"baseline": str(defaults.get(k)), "variant": str(v)}
            for k, v in options.items() if str(v) != str(defaults.get(k))}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def recordings(config: Config, participant: str, season: str,
               device: str = "actigraph") -> list[dict[str, Any]]:
    """Baseline recordings in one participant-season that a variant could be made from.

    A recording is recomputable when its raw ``.bin`` is present in the master
    folder. Everything needed by the UI comes back on each row, including why a
    recording cannot be used.
    """
    dev_dir = device_dir(config, participant, season, device)
    out: list[dict[str, Any]] = []
    if not dev_dir.is_dir():
        return out
    for epoch_csv in sorted(dev_dir.glob("*_60s.csv")):
        stem = epoch_csv.name[: -len("_60s.csv")]
        raw = dev_dir / f"{stem}.bin"
        verdict = None
        comp = dev_dir / f"{stem}_compliance.json"
        if comp.is_file():
            try:
                verdict = (json.loads(comp.read_text(encoding="utf-8"))
                           .get("compliance", {}).get("verdict"))
            except (OSError, ValueError):
                pass
        out.append({
            "participant": participant, "season": season, "device": device,
            "stem": stem,
            "verdict": verdict,
            "raw_present": raw.is_file(),
            "raw_mb": round(raw.stat().st_size / (1024 ** 2), 1) if raw.is_file() else None,
            "blocked": None if raw.is_file() else
                       "the raw .bin is not in the master folder - run a force reprocess "
                       "for this recording first (recompute never downloads from OneDrive)",
            "variants": list_variants(config, participant, season, device, stem),
        })
    return out


def list_variants(config: Config, participant: str, season: str,
                  device: str = "actigraph", stem: Optional[str] = None) -> list[dict[str, Any]]:
    root = variants_dir(config, participant, season, device)
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name != DISCARDED_DIRNAME):
        record = folder / "variant.json"
        if not record.is_file():
            continue
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if stem and data.get("input_stem") != stem:
            continue
        csv_name = data.get("output_csv") or ""
        data["exists"] = bool(csv_name) and (folder / csv_name).is_file()
        data["folder"] = str(folder)
        out.append(data)
    return out


def read_variant(config: Config, participant: str, season: str, device: str,
                 variant_id: str) -> dict[str, Any]:
    if not _ID_RE.match(variant_id or ""):
        raise VariantError("invalid variant id")
    record = variants_dir(config, participant, season, device) / variant_id / "variant.json"
    if not record.is_file():
        raise VariantError(f"no such variant: {variant_id}")
    return json.loads(record.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# making one
# ---------------------------------------------------------------------------
def create(
    config: Config,
    participant: str,
    season: str,
    stem: str,
    options: dict[str, str],
    initials: str,
    bus: EventBus,
    device: str = "actigraph",
    batch_id: Optional[str] = None,
    cohort: Optional[str] = None,
) -> dict[str, Any]:
    """Run Step 1 with *options* and record the result. Returns the variant record.

    Raises VariantError before running anything if the request is unusable, so
    a caller can validate a whole batch up front.
    """
    if device not in SUPPORTED_DEVICES:
        raise VariantError(f"recompute is only available for {', '.join(SUPPORTED_DEVICES)}")
    opts = tools.normalise_step1_options(options)
    changed = changed_from_baseline(opts)
    if not changed:
        raise VariantError("these are the baseline settings - change at least one "
                           "to make a variant")

    dev_dir = device_dir(config, participant, season, device)
    raw = dev_dir / f"{stem}.bin"
    if not raw.is_file():
        raise VariantError(
            f"{stem}: the raw .bin is not in the master folder. Recompute reads the "
            "local copy and never downloads from OneDrive - run a force reprocess "
            "for this recording first.")

    label = tools.epoch_label(opts["epoch"])
    if label == tools.epoch_label(tools.step1_defaults()["epoch"]) and "epoch" not in changed:
        pass  # a non-epoch variant keeps the baseline epoch label; folder keeps them apart

    started = datetime.now()
    variant_id = make_variant_id(opts, initials, started)
    folder = variants_dir(config, participant, season, device) / variant_id
    if folder.exists():
        raise VariantError(f"variant {variant_id} already exists")
    folder.mkdir(parents=True)

    bus.emit("variant_start", label=f"{participant} / {season} / {stem}",
             variant_id=variant_id, changed=changed)
    try:
        epoch_csv = tools.run_step1_process(
            config, raw, folder, bus, options=opts, name="recompute:step1")
    except Exception:
        # Never leave a half-written variant folder behind - nor an empty
        # `_variants/` parent, so a failed recompute adds nothing to master.
        shutil.rmtree(folder, ignore_errors=True)
        try:
            folder.parent.rmdir()
        except OSError:
            pass                      # other variants live here; leave it alone
        raise

    record = {
        "schema_version": 1,
        "variant_id": variant_id,
        "kind": "step1",
        "experimental": True,
        "participant": participant,
        "season": season,
        "device": device,
        "input_stem": stem,
        "output_csv": epoch_csv.name,
        "step1_options": opts,
        "changed": changed,
        "step2": "not run - press 'run step 2' on this variant if you want it",
        "compliance": "not evaluated - the 60 s baseline verdict applies",
        "batch_id": batch_id,
        "cohort": cohort,
        "by": initials,
        "at": started.isoformat(timespec="seconds"),
        "tool_versions": _tool_versions(config),
    }
    if tools.epoch_label(opts["epoch"]) != "60s":
        record["warnings"] = [
            "SRI: Cole-Kripke weights are defined for 60 s epochs, so the kernel spans "
            "half the real time at 30 s and biases SRI downward.",
            "M10/L5 activity levels and the Enright amplitude scale with epoch length "
            "(SVM_sum is a sum) and are not comparable with the 60 s baseline. "
            "Hours, IS, IV and the chi-square periodogram are comparable.",
        ]
    (folder / "variant.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    bus.emit("variant_done", label=f"{participant} / {season} / {stem}",
             variant_id=variant_id, output=epoch_csv.name)
    record["folder"] = str(folder)
    record["exists"] = True
    return record


def _tool_versions(config: Config) -> dict[str, Optional[str]]:
    from . import updates

    out: dict[str, Optional[str]] = {}
    for spec in updates.component_specs(config):
        ok, commit = updates._git(["rev-parse", "--short", "HEAD"], Path(spec["path"]), timeout=10)
        out[spec["key"]] = commit if ok else None
    return out


# ---------------------------------------------------------------------------
# removing one
# ---------------------------------------------------------------------------
def discard(config: Config, participant: str, season: str, device: str,
            variant_id: str, initials: str, reason: str = "") -> dict[str, Any]:
    """Move a variant to ``_variants/_discarded/`` - never delete it outright.

    A study audit should be able to answer "what happened to that 30 s run?"
    with something other than silence, so this moves and annotates rather than
    unlinking. The baseline files are never touched.
    """
    if not _ID_RE.match(variant_id or ""):
        raise VariantError("invalid variant id")
    root = variants_dir(config, participant, season, device)
    folder = root / variant_id
    if not folder.is_dir():
        raise VariantError(f"no such variant: {variant_id}")

    record_path = folder / "variant.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    record["discarded"] = {"by": initials, "at": datetime.now().isoformat(timespec="seconds"),
                           "reason": reason[:300]}
    try:
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError:
        pass

    dest_root = root / DISCARDED_DIRNAME
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / variant_id
    if dest.exists():
        dest = dest_root / f"{variant_id}-{datetime.now():%H%M%S}"
    shutil.move(str(folder), str(dest))
    record["folder"] = str(dest)
    return record
