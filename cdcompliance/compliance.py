"""Actigraph compliance evaluation.

Wear-validity rule aligned with actigraphy best practice, computed from the
Step 1 60-second epoch CSV (which carries SVM_sum and per-epoch axis SDs):

1. Non-wear detection (van Hees / GGIR style). A worn wrist always has some
   micro-movement, so its per-axis acceleration SD stays above a small floor; a
   device that is off or resting on a surface is essentially motionless (SD ~ 0).
   An epoch is a non-wear *candidate* when >= 2 of 3 axis SDs are below
   `nonwear_axis_sd_g`, and it counts as non-wear only inside a sustained run of
   >= `nonwear_min_minutes` (so ordinary stillness / sleep is not flagged).

2. Valid day. A day is valid when it has >= `min_valid_hours` of WEAR time AND a
   real rest/activity rhythm — its hourly-mean SVM must span at least
   `min_diurnal_range_svm`. A near-constant ("flat") day is physiologically
   implausible for a worn device and is not valid even if the level is high.
   This is what catches "worn then dormant" / constant-signal recordings.

3. Verdict. PASS when valid_days >= min_valid_days; REVIEW within the margin;
   otherwise FAIL.

If the axis-SD columns are absent (SVM-only input) the rule falls back to a
simple SVM level threshold and says so.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import ComplianceConfig
from .models import ComplianceResult


def _read_epoch_csv(csv_path: Path, cfg: ComplianceConfig) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if cfg.svm_column not in df.columns:
        raise ValueError(
            f"Expected column '{cfg.svm_column}' not found in {csv_path.name}. "
            f"Columns: {list(df.columns)}"
        )
    if cfg.time_column not in df.columns:
        raise ValueError(
            f"Expected column '{cfg.time_column}' not found in {csv_path.name}."
        )
    # Step 1 writes 'YYYY-MM-DD HH:MM:SS:mmm' (millis delimited by ':').
    times = df[cfg.time_column].astype(str).str.replace(r":(\d{3})$", r".\1", regex=True)
    df["_time"] = pd.to_datetime(times, errors="coerce")
    df["_svm"] = pd.to_numeric(df[cfg.svm_column], errors="coerce")
    for c in cfg.axis_sd_columns:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["_time"]).sort_values("_time").reset_index(drop=True)
    return df


def _sustained(candidate: np.ndarray, min_epochs: int) -> np.ndarray:
    """True only within runs of `candidate` of length >= min_epochs."""
    candidate = np.asarray(candidate, dtype=bool)
    out = np.zeros_like(candidate)
    n = len(candidate)
    i = 0
    while i < n:
        if candidate[i]:
            j = i
            while j < n and candidate[j]:
                j += 1
            if (j - i) >= min_epochs:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def evaluate(csv_path: Path, cfg: ComplianceConfig) -> ComplianceResult:
    csv_path = Path(csv_path)
    df = _read_epoch_csv(csv_path, cfg)

    if df.empty:
        return ComplianceResult(
            device="actigraph", verdict="ERROR",
            summary={"reason": "no parseable epochs"}, parameters=_params(cfg),
        )

    axis_cols = [c for c in cfg.axis_sd_columns if c in df.columns]
    have_axis = len(axis_cols) >= 2

    if have_axis:
        method = f"non-wear via axis SD < {cfg.nonwear_axis_sd_g} g, sustained >= {cfg.nonwear_min_minutes} min"
        below = np.zeros(len(df), dtype=int)
        for c in axis_cols:
            below += (df[c].to_numpy(dtype=float) < cfg.nonwear_axis_sd_g).astype(int)
        candidate = below >= 2  # >= 2 of 3 axes essentially motionless
        # 60-s epochs => 1 epoch == 1 minute, so min run length == min_minutes.
        nonwear = _sustained(candidate, cfg.nonwear_min_minutes)
        df["_wear"] = ~nonwear
    else:
        method = f"SVM level threshold >= {cfg.activity_threshold_svm} (fallback; axis SDs absent)"
        df["_wear"] = df["_svm"] >= cfg.activity_threshold_svm

    df["_date"] = df["_time"].dt.date
    df["_hour"] = df["_time"].dt.hour
    expected_wear = cfg.expected_wear_hours if cfg.expected_wear_hours > 0 else 24.0

    per_day: list[dict] = []
    valid_days = 0
    for date, g in df.groupby("_date", sort=True):
        wear_epochs = int(g["_wear"].sum())
        wear_hours = wear_epochs / cfg.epochs_per_hour
        nonwear_hours = (len(g) - wear_epochs) / cfg.epochs_per_hour
        svm = g["_svm"].to_numpy(dtype=float)
        hourly = g.groupby("_hour")["_svm"].mean()
        hourly_range = float(hourly.max() - hourly.min()) if len(hourly) else 0.0
        within_sd = float(np.nanstd(svm))

        enough_wear = wear_hours >= cfg.min_valid_hours
        has_rhythm = hourly_range >= cfg.min_diurnal_range_svm
        is_valid = enough_wear and has_rhythm
        if is_valid:
            valid_days += 1

        reasons = []
        if not enough_wear:
            reasons.append(f"wear {wear_hours:.1f}h < {cfg.min_valid_hours:g}h")
        if not has_rhythm:
            reasons.append(f"flat: hourly range {hourly_range:.0f} < {cfg.min_diurnal_range_svm:g}")

        pct = min(100.0, wear_hours / expected_wear * 100.0)
        per_day.append({
            "date": str(date),
            "epochs": int(len(g)),
            "wear_hours": round(wear_hours, 3),
            "nonwear_hours": round(nonwear_hours, 3),
            "pct_compliance": round(pct, 2),
            "hourly_svm_range": round(hourly_range, 2),
            "within_day_svm_sd": round(within_sd, 3),
            "svm_mean": round(float(np.nanmean(svm)), 4),
            "valid_day": bool(is_valid),
            "invalid_reason": "; ".join(reasons),
        })

    total_days = len(per_day)
    if valid_days >= cfg.min_valid_days:
        verdict = "PASS"
    elif valid_days >= (cfg.min_valid_days - cfg.review_margin_days):
        verdict = "REVIEW"
    else:
        verdict = "FAIL"

    all_svm = df["_svm"].to_numpy(dtype=float)
    daily_pct = [d["pct_compliance"] for d in per_day]
    wear_epochs_total = int(df["_wear"].sum())
    summary = {
        "verdict": verdict,
        "valid_days": valid_days,
        "total_days": total_days,
        "min_valid_days": cfg.min_valid_days,
        "wear_detection": method,
        "total_epochs": int(len(df)),
        "wear_epochs": wear_epochs_total,
        "nonwear_pct": round(100.0 * (1.0 - df["_wear"].mean()), 1),
        "total_wear_hours": round(wear_epochs_total / cfg.epochs_per_hour, 2),
        "recording_start": str(df["_time"].min()),
        "recording_end": str(df["_time"].max()),
        "pct_compliance_mean": round(float(np.mean(daily_pct)), 2) if daily_pct else 0.0,
        "expected_wear_hours": expected_wear,
        "svm_mean": round(float(np.nanmean(all_svm)), 5),
        "svm_sd": round(float(np.nanstd(all_svm)), 5),
        "svm_median": round(float(np.nanmedian(all_svm)), 5),
        "svm_p90": round(float(np.nanpercentile(all_svm, 90)), 5),
        "svm_p99": round(float(np.nanpercentile(all_svm, 99)), 5),
    }

    notes = []
    if not have_axis:
        notes.append("Axis SD columns absent — used SVM level fallback (weaker). "
                     "Re-export Step 1 in full-summary mode for proper non-wear detection.")
    flat_days = sum(1 for d in per_day if "flat" in d["invalid_reason"])
    if flat_days:
        notes.append(f"{flat_days} day(s) rejected as flat/constant (implausible for a worn device).")

    return ComplianceResult(
        device="actigraph", verdict=verdict, summary=summary,
        per_day=per_day, parameters=_params(cfg), notes=notes,
    )


def _params(cfg: ComplianceConfig) -> dict:
    return {
        "rule": "wear-validity (non-wear + valid-day + diurnal variation)",
        "nonwear_axis_sd_g": cfg.nonwear_axis_sd_g,
        "nonwear_min_minutes": cfg.nonwear_min_minutes,
        "min_valid_hours": cfg.min_valid_hours,
        "min_valid_days": cfg.min_valid_days,
        "review_margin_days": cfg.review_margin_days,
        "min_diurnal_range_svm": cfg.min_diurnal_range_svm,
        "epochs_per_hour": cfg.epochs_per_hour,
        "expected_wear_hours": cfg.expected_wear_hours,
        "svm_column": cfg.svm_column,
    }


def write_daily_compliance_csv(result: ComplianceResult, out_csv: Path) -> Path:
    """Write the per-day compliance table (incl. pct_compliance) to CSV.

    This is the daily report CSV the dashboard reads for per-day / per-season
    %compliance visualisation.
    """
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(result.per_day).to_csv(out_csv, index=False)
    return out_csv
