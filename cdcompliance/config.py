"""Configuration model and loader.

All environment-specific values (paths, the Python interpreter, and the
compliance thresholds) live in a YAML config file so the pipeline logic stays
free of hard-coded machine paths. `RunSelection` layers per-run choices
(participant, seasons, devices) on top — a future web UI builds a `RunSelection`
from user input and hands it to the same core.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "PyYAML is required. Install project deps: pip install -r requirements.txt"
    ) from exc


DEVICE_ACTIGRAPH = "actigraph"
ALL_DEVICES = (
    DEVICE_ACTIGRAPH,
    "expiwell",
    "saliva",
    "cognitron",
    "qualtrics",
    "mieye",
)


@dataclass
class ToolsConfig:
    # Repo dir of Step 1 (actigraphy-epoching). Tools are run as `python -m cli`
    # with cwd set to these dirs, because they rely on bare/relative imports.
    epoching_repo: Path
    # Repo dir of Step 2 (actigraphy-sleep-metrics).
    sleep_metrics_repo: Path
    # Interpreter used to launch both tools. Must have numpy/pandas/scipy/
    # matplotlib (and Step 2's deps) importable.
    python_executable: str = "python"


@dataclass
class PathsConfig:
    # Parent folder that contains participant folders (CD011, CD012, ...).
    source_root: Path
    # Dashboard output tree root. Outputs land in
    #   <output_root>/<participant>/<season>/<Device>/
    output_root: Path


@dataclass
class ComplianceConfig:
    """Actigraph 'valid-day wear' rule parameters.

    An epoch counts as *active* when its SVM_sum >= ``activity_threshold_svm``.
    A day is *valid* when it has at least ``min_valid_hours`` of active epochs.
    A recording PASSES with >= ``min_valid_days`` valid days; it is REVIEW when
    within ``review_margin_days`` of that bar; otherwise FAIL.

    NOTE / CALIBRATE: ``activity_threshold_svm`` depends on the device sample
    rate and the GENEActiv-style SVM definition (sum of abs(vector_magnitude-1)
    per 60-s epoch). The default below is a placeholder — review the per-day
    SVM distribution the pipeline reports and tune this against a night of known
    wear/non-wear before treating verdicts as authoritative.
    """

    # --- Non-wear detection (van Hees / GGIR style) --------------------------
    # A worn wrist always has micro-movement, so acceleration SD stays above a
    # small floor; a device that is off / on a bench is essentially motionless
    # (SD ~ 0). An epoch is a non-wear *candidate* when at least 2 of 3 axis SDs
    # are below this threshold (in g; 0.013 g = 13 mg is the van Hees value).
    nonwear_axis_sd_g: float = 0.013
    # Candidate epochs only count as non-wear when they form a sustained run of
    # at least this many minutes (so normal still periods / sleep are not flagged).
    nonwear_min_minutes: int = 30

    # --- Valid day / recording ----------------------------------------------
    min_valid_hours: float = 10.0     # wear hours needed for a day to be valid
    min_valid_days: int = 3           # valid days needed to PASS
    review_margin_days: int = 1
    # Diurnal-variation guard: a genuinely-worn day has a rest/activity rhythm,
    # so its hourly-mean SVM spans a real range. A near-constant day (flat signal)
    # is physiologically implausible for a worn device -> not a valid day even if
    # the level is high. Range of hourly-mean SVM must exceed this.
    min_diurnal_range_svm: float = 20.0

    epochs_per_hour: int = 60  # 60-second epochs
    # Denominator for daily %compliance: pct = wear_hours / expected_wear_hours.
    expected_wear_hours: float = 16.0

    svm_column: str = "SVM_sum"
    time_column: str = "Time"
    # Per-epoch axis SD columns (present in Step 1 full-summary output). When
    # absent, non-wear detection falls back to an SVM level threshold.
    axis_sd_columns: tuple = ("Ax_sd", "Ay_sd", "Az_sd")
    activity_threshold_svm: float = 5.0  # fallback only (svm-only inputs)


@dataclass
class OneDriveConfig:
    poll_interval_seconds: float = 1.0
    stall_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 3600.0


@dataclass
class Config:
    paths: PathsConfig
    tools: ToolsConfig
    compliance: ComplianceConfig = field(default_factory=ComplianceConfig)
    onedrive: OneDriveConfig = field(default_factory=OneDriveConfig)

    # Run defaults (overridable per run / via CLI).
    participant: str = "CD011"
    devices: list[str] = field(default_factory=lambda: [DEVICE_ACTIGRAPH])
    seasons: Optional[list[str]] = None  # None => all discovered
    copy_bin: bool = True  # replicate the raw .bin into the output tree
    actigraph_bin_glob: str = "*.bin"
    actigraph_folder_name: str = "Actigraph"

    # Directory (created if absent) for per-run event logs and summaries.
    runs_dir: Path = Path("runs")


def _as_path(value: Any, base: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_config(config_path: Path) -> Config:
    config_path = Path(config_path)
    base = config_path.parent
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    paths_raw = raw.get("paths", {})
    tools_raw = raw.get("tools", {})
    comp_raw = raw.get("compliance", {})
    od_raw = raw.get("onedrive", {})

    paths = PathsConfig(
        source_root=_as_path(paths_raw["source_root"], base),
        output_root=_as_path(paths_raw["output_root"], base),
    )
    tools = ToolsConfig(
        epoching_repo=_as_path(tools_raw["epoching_repo"], base),
        sleep_metrics_repo=_as_path(tools_raw["sleep_metrics_repo"], base),
        python_executable=tools_raw.get("python_executable", "python"),
    )
    compliance = ComplianceConfig(
        **{k: v for k, v in comp_raw.items() if k in _field_names(ComplianceConfig)}
    )
    onedrive = OneDriveConfig(
        **{k: v for k, v in od_raw.items() if k in _field_names(OneDriveConfig)}
    )

    runs_dir = raw.get("runs_dir", "runs")
    return Config(
        paths=paths,
        tools=tools,
        compliance=compliance,
        onedrive=onedrive,
        participant=raw.get("participant", "CD011"),
        devices=list(raw.get("devices", [DEVICE_ACTIGRAPH])),
        seasons=raw.get("seasons"),
        copy_bin=bool(raw.get("copy_bin", True)),
        actigraph_bin_glob=raw.get("actigraph_bin_glob", "*.bin"),
        actigraph_folder_name=raw.get("actigraph_folder_name", "Actigraph"),
        runs_dir=_as_path(runs_dir, base),
    )


def _field_names(dc: type) -> set[str]:
    return {f.name for f in dataclasses.fields(dc)}


@dataclass
class RunSelection:
    """Per-run choices; None values fall back to the Config defaults."""

    participant: Optional[str] = None
    seasons: Optional[list[str]] = None
    devices: Optional[list[str]] = None
    copy_bin: Optional[bool] = None
    dry_run: bool = False
    # When False (default) items whose expected output already exists are skipped
    # (selective processing). Set True to reprocess regardless.
    force: bool = False

    def resolve(self, config: Config) -> "ResolvedSelection":
        return ResolvedSelection(
            participant=self.participant or config.participant,
            seasons=self.seasons if self.seasons is not None else config.seasons,
            devices=[d.lower() for d in (self.devices or config.devices)],
            copy_bin=config.copy_bin if self.copy_bin is None else self.copy_bin,
            dry_run=self.dry_run,
            force=self.force,
        )


@dataclass
class ResolvedSelection:
    participant: str
    seasons: Optional[list[str]]
    devices: list[str]
    copy_bin: bool
    dry_run: bool
    force: bool = False
