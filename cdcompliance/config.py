"""Configuration model and loader.

All environment-specific values (paths, the Python interpreter, and the
compliance thresholds) live in a YAML config file so the pipeline logic stays
free of hard-coded machine paths. `RunSelection` layers per-run choices
(participant, seasons, devices) on top — a future web UI builds a `RunSelection`
from user input and hands it to the same core.
"""
from __future__ import annotations

import dataclasses
import sys
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
    # Local repo dir of Step 1 (actigraphy-epoching). Tools are run as
    # `python -m cli` with cwd set to these dirs (they use bare/relative imports).
    # setup_tools.py clones these from the *_url below if missing.
    epoching_repo: Path
    # Local repo dir of Step 2 (actigraphy-sleep-metrics).
    sleep_metrics_repo: Path
    # Local repo dir of the MiEYE luminosity tool (luminosity-metrics).
    luminosity_repo: Path
    # Local repo dir of the Expiwell ESM tool (expiwell-metrics).
    expiwell_repo: Path
    # Interpreter used to launch the tools. Must have numpy/pandas/scipy/
    # matplotlib (and each tool's deps) importable.
    python_executable: str = "python"
    # GitHub sources — setup_tools.py fetches the latest from here on install.
    epoching_repo_url: str = "https://github.com/liyang-D/actigraphy-epoching.git"
    sleep_metrics_repo_url: str = "https://github.com/infernalzeus/actigraphy-sleep-metrics.git"
    luminosity_repo_url: str = "https://github.com/infernalzeus/luminosity-metrics.git"
    expiwell_repo_url: str = "https://github.com/infernalzeus/expiwell-metrics.git"


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
class LuminosityComplianceConfig:
    """MiEYE luminosity valid-day rule parameters.

    The device is charged regularly, so the charger flag is NOT a wear signal.
    A day is valid when it shows diurnal variation (daytime melanopic brighter
    than night by ``min_day_night_ratio``) AND has at least ``min_light_hours`` of
    light-activity (melanopic >= ``light_floor_lx``); ``expected_light_hours`` ≈ 16
    is the daily %compliance target. The day/night windows and TAT/TBT thresholds
    feed the secondary light-adequacy stats (Brown et al. 2022). All soft /
    calibratable, passed through to the luminosity-metrics CLI which owns the
    computation.
    """

    min_valid_days: int = 3
    review_margin_days: int = 1
    light_floor_lx: float = 250.0      # daytime adequacy floor (melanopic lx)
    min_light_hours: float = 10.0      # hours at/above the floor for a valid day
    expected_light_hours: float = 10.0 # daily %compliance denominator
    dark_threshold_lx: float = 200.0   # 'dark / rest' boundary (hourly histogram, L windows)
    min_day_night_ratio: float = 2.0
    channel: str = "melanopic"
    day_window: tuple = (7, 19)
    night_window: tuple = (23, 6)
    tat_threshold_lx: float = 250.0
    tbt_threshold_lx: float = 10.0


@dataclass
class ExpiwellComplianceConfig:
    """Expiwell ESM response-rate rule parameters.

    An ExpiWell export lists only *completed* responses, so the denominator (how
    many prompts were scheduled) is configured, not inferred — ``expected_days``
    plus the per-survey protocol inside expiwell-metrics (override it per study
    with ``schedule_file``). A recording PASSES when the pooled response rate
    across the scored surveys reaches ``min_response_rate_pct``.
    """

    min_response_rate_pct: float = 70.0
    review_margin_pct: float = 10.0
    expected_days: int = 15
    #: Responses faster than this are flagged as possible straight-lining.
    min_plausible_duration_sec: float = 5.0
    #: Optional JSON overriding the per-survey schedule (see the tool's
    #: survey_schedule.example.json). Empty => the tool's built-in defaults.
    schedule_file: str = ""


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
    luminosity: LuminosityComplianceConfig = field(default_factory=LuminosityComplianceConfig)
    expiwell: ExpiwellComplianceConfig = field(default_factory=ExpiwellComplianceConfig)
    onedrive: OneDriveConfig = field(default_factory=OneDriveConfig)

    # Run defaults (overridable per run / via CLI).
    participant: str = "CD011"
    devices: list[str] = field(default_factory=lambda: [DEVICE_ACTIGRAPH])
    seasons: Optional[list[str]] = None  # None => all discovered
    copy_bin: bool = True  # replicate the raw .bin into the output tree
    actigraph_bin_glob: str = "*.bin"
    actigraph_folder_name: str = "Actigraph"
    mieye_folder_name: str = "MiEYE"
    mieye_input_glob: str = "*-logged.csv"
    expiwell_folder_name: str = "Expiwell"
    expiwell_input_glob: str = "*Expiwell*.csv"

    # Directory (created if absent) for per-run event logs and summaries.
    runs_dir: Path = Path("runs")


def _as_path(value: Any, base: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_config(config_path: Path) -> Config:
    # Resolve first: sibling tool paths are derived from the config file's
    # PARENT directory, which is wrong for a relative path like 'config.yaml'.
    config_path = Path(config_path).expanduser().resolve()
    base = config_path.parent
    # Tool repos default to siblings of the project folder: if CD-Compliance-Checks
    # lives at  <parent>/CD-Compliance-Checks,  the tools go to  <parent>/<tool>.
    parent = base.parent

    # A missing config.yaml is fine — everything falls back to portable defaults,
    # so a fresh clone runs on any machine (set the data folders from the ⚙ panel).
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    else:
        raw = {}

    paths_raw = raw.get("paths") or {}
    tools_raw = raw.get("tools") or {}
    comp_raw = raw.get("compliance") or {}
    lum_raw = raw.get("luminosity") or {}
    xpw_raw = raw.get("expiwell") or {}
    od_raw = raw.get("onedrive") or {}

    # source/output are inherently machine-specific (OneDrive paths); if unset the
    # app still boots — the grid is empty and the ⚙ panel prompts for them.
    paths = PathsConfig(
        source_root=_as_path(paths_raw.get("source_root") or "unset-source-root", base),
        output_root=_as_path(paths_raw.get("output_root") or "unset-output-root", base),
    )
    tools = ToolsConfig(
        epoching_repo=_as_path(tools_raw.get("epoching_repo") or (parent / "actigraphy-epoching"), base),
        sleep_metrics_repo=_as_path(tools_raw.get("sleep_metrics_repo") or (parent / "actigraphy-sleep-metrics"), base),
        luminosity_repo=_as_path(tools_raw.get("luminosity_repo") or (parent / "luminosity-metrics"), base),
        expiwell_repo=_as_path(tools_raw.get("expiwell_repo") or (parent / "expiwell-metrics"), base),
        # Default to the interpreter running the app (sys.executable) so the tools
        # use the same Python/venv — avoids "python not found" on macOS/Linux where
        # only `python3` exists. Override in config to pin a specific env.
        python_executable=tools_raw.get("python_executable") or sys.executable,
        epoching_repo_url=tools_raw.get(
            "epoching_repo_url", "https://github.com/liyang-D/actigraphy-epoching.git"
        ),
        sleep_metrics_repo_url=tools_raw.get(
            "sleep_metrics_repo_url",
            "https://github.com/infernalzeus/actigraphy-sleep-metrics.git",
        ),
        expiwell_repo_url=tools_raw.get(
            "expiwell_repo_url",
            "https://github.com/infernalzeus/expiwell-metrics.git",
        ),
        luminosity_repo_url=tools_raw.get(
            "luminosity_repo_url",
            "https://github.com/infernalzeus/luminosity-metrics.git",
        ),
    )
    compliance = ComplianceConfig(
        **{k: v for k, v in comp_raw.items() if k in _field_names(ComplianceConfig)}
    )
    luminosity = LuminosityComplianceConfig(
        **{k: v for k, v in lum_raw.items() if k in _field_names(LuminosityComplianceConfig)}
    )
    expiwell = ExpiwellComplianceConfig(
        **{k: v for k, v in xpw_raw.items() if k in _field_names(ExpiwellComplianceConfig)}
    )
    onedrive = OneDriveConfig(
        **{k: v for k, v in od_raw.items() if k in _field_names(OneDriveConfig)}
    )

    runs_dir = raw.get("runs_dir", "runs")
    return Config(
        paths=paths,
        tools=tools,
        compliance=compliance,
        luminosity=luminosity,
        expiwell=expiwell,
        onedrive=onedrive,
        participant=raw.get("participant", "CD011"),
        devices=list(raw.get("devices", [DEVICE_ACTIGRAPH])),
        seasons=raw.get("seasons"),
        copy_bin=bool(raw.get("copy_bin", True)),
        actigraph_bin_glob=raw.get("actigraph_bin_glob", "*.bin"),
        actigraph_folder_name=raw.get("actigraph_folder_name", "Actigraph"),
        mieye_folder_name=raw.get("mieye_folder_name", "MiEYE"),
        mieye_input_glob=raw.get("mieye_input_glob", "*-logged.csv"),
        expiwell_folder_name=raw.get("expiwell_folder_name", "Expiwell"),
        expiwell_input_glob=raw.get("expiwell_input_glob", "*Expiwell*.csv"),
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
    # threading.Event set when the user presses STOP or the server shuts down.
    # Device processors pass it to the long-running stages (OneDrive hydration,
    # large copies) so a run can be interrupted mid-download instead of only
    # between items. None => not cancellable (plain CLI use).
    cancel_event: Optional[Any] = None
