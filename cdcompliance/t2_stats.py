"""T2 phase P2: cross-device pairing and correlations.

Days repeat within each person, so one pooled correlation would mix two very
different questions. Every pair is therefore reported twice:

**between-person** - correlate each participant's mean x against their mean y.
n = participants. Answers "do people with more of x tend to have more of y?"

**within-person** - subtract each participant's own mean from their days, then
correlate the deviations. n = paired days, but the effective sample size is
``n_pairs - n_participants + 1`` because each person's mean is estimated from
their own data. Answers "on days when this person has more x than usual, do
they also have more y than usual?"

Confidence intervals and p-values use the Fisher z transform with a normal
approximation (scipy is not a dependency of the dashboard); with the sample
sizes here that is close enough for an exploratory preview, and the export
records that these are exploratory. Multiple pairs are corrected with
Benjamini-Hochberg FDR (``q``).

Lags line the two devices up in time:

    same   the same calendar date
    night  y belongs to the night that starts on x's date (night_of == x.date)
    next   y is the following calendar date
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

LAGS = {
    "same": "same day",
    "night": "that night (night starting on x's date)",
    "next": "next day",
}
#: Pairs with fewer than this many points get no correlation at all.
MIN_POINTS = 4
#: A participant needs this many paired days before their own r is computed.
MIN_DAYS_PER_PERSON = 5
#: Fewer people than this and a between-person correlation means nothing.
MIN_PARTICIPANTS_BETWEEN = 3


# ---------------------------------------------------------------------------
# filtering and pairing
# ---------------------------------------------------------------------------
def day_frame(long: pd.DataFrame, *, weeks: Optional[tuple[int, int]] = None,
              days: Optional[tuple[int, int]] = None, cutoffs: Optional[list] = None,
              in_window_only: bool = True, valid_only: bool = False,
              exclude_partial: bool = True) -> pd.DataFrame:
    """Day-level rows after the preview's filters.

    ``days`` is a range of days within each participant-season's common window
    (day 1 = its first shared day); ``weeks`` is the same thing in sevens.
    """
    d = long[(long["level"] == "day") & long["date"].notna()]
    if exclude_partial:
        d = d[d["partial"] != True]        # noqa: E712
    if valid_only:                     # kept for callers outside T2; T2 passes False
        d = d[d["valid"] != False]         # noqa: E712  (None = device reports none)
    if in_window_only:
        d = d[d["in_window"] == True]      # noqa: E712
    span = days or ((weeks[0] - 1) * 7 + 1, weeks[1] * 7) if (days or weeks) else None
    if span:
        wd = pd.to_numeric(d["window_day"], errors="coerce")
        d = d[(wd >= span[0]) & (wd <= span[1])]
    if cutoffs:
        d = apply_cutoffs(d, cutoffs)
    return d


OPERATORS = {">=": "at least", "<=": "at most", ">": "more than", "<": "less than"}


def apply_cutoffs(d: pd.DataFrame, cutoffs: list[dict[str, Any]]) -> pd.DataFrame:
    """Keep only the days that meet every cut-off.

    A cut-off names one measure, so a day that has no value for that measure
    cannot be judged and is dropped. Cut-offs stack (all must hold).
    """
    for rule in cutoffs or []:
        variable, op = rule.get("variable"), rule.get("op", ">=")
        try:
            value = float(rule.get("value"))
        except (TypeError, ValueError):
            continue
        if not variable or op not in OPERATORS:
            continue
        g = d[d["variable"] == variable]
        vals = pd.to_numeric(g["value"], errors="coerce")
        keep = {">=": vals >= value, "<=": vals <= value,
                ">": vals > value, "<": vals < value}[op]
        good = set(map(tuple, g[keep][["participant", "season", "date"]].to_numpy()))
        if not good:
            return d.iloc[0:0]
        keys = list(map(tuple, d[["participant", "season", "date"]].to_numpy()))
        d = d[[k in good for k in keys]]
    return d


def measure_ranges(long: pd.DataFrame, variables=None, **filters) -> list[dict[str, Any]]:
    """Min / median / max per measure, so a slider knows where to start."""
    d = day_frame(long, **filters)
    if variables:
        d = d[d["variable"].isin(list(variables))]
    rows = []
    for var, g in d.groupby("variable", sort=False):
        vals = pd.to_numeric(g["value"], errors="coerce").dropna()
        if vals.empty:
            continue
        rows.append({
            "variable": var, "device": g["device"].iloc[0], "n_days": int(len(vals)),
            "min": round(float(vals.min()), 3), "max": round(float(vals.max()), 3),
            "median": round(float(vals.median()), 3),
            "p10": round(float(vals.quantile(0.10)), 3),
            "p90": round(float(vals.quantile(0.90)), 3),
        })
    return rows


def _one(d: pd.DataFrame, variable: str, value_name: str, key: str) -> pd.DataFrame:
    g = d[d["variable"] == variable]
    if g.empty:
        return pd.DataFrame(columns=["participant", "display_id", "season", "season_n", "_key", value_name])
    out = g[["participant", "display_id", "season", "season_n", "date", "night_of", "value"]].copy()
    if key == "night_of":
        out["_key"] = out["night_of"]
    elif key == "date-1":
        out["_key"] = [None if pd.isna(x) else x - timedelta(days=1) for x in out["date"]]
    else:
        out["_key"] = out["date"]
    out = out.rename(columns={"value": value_name})
    out = out.dropna(subset=["_key", value_name])
    # one value per day per variable (repeated recordings in a season)
    return (out.groupby(["participant", "display_id", "season", "season_n", "_key"], as_index=False)[value_name]
               .mean())


def pair(long: pd.DataFrame, x: str, y: str, lag: str = "same", **filters) -> pd.DataFrame:
    """Paired day-level values for two variables under a lag."""
    if lag not in LAGS:
        raise ValueError(f"unknown lag {lag!r}")
    d = day_frame(long, **filters)
    left = _one(d, x, "x", "date")
    right_key = {"same": "date", "night": "night_of", "next": "date-1"}[lag]
    right = _one(d, y, "y", right_key)
    if left.empty or right.empty:
        return pd.DataFrame(columns=["participant", "display_id", "season", "season_n", "date", "x", "y"])
    merged = left.merge(right[["participant", "season", "_key", "y"]],
                        on=["participant", "season", "_key"], how="inner")
    return merged.rename(columns={"_key": "date"}).sort_values(["participant", "season", "date"])


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------
def _norm_p(z_abs: float) -> float:
    """Two-sided p from a standard normal deviate."""
    return float(math.erfc(z_abs / math.sqrt(2.0)))


def _pearson(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _fisher(r: Optional[float], n_eff: float) -> dict[str, Any]:
    """r with a Fisher-z 95 % CI and p (normal approximation)."""
    out: dict[str, Any] = {"r": None if r is None else round(r, 3), "ci": None, "p": None}
    if r is None or n_eff <= 3 or abs(r) >= 1:
        return out
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n_eff - 3)
    out["ci"] = [round(math.tanh(z - 1.96 * se), 3), round(math.tanh(z + 1.96 * se), 3)]
    out["p"] = round(_norm_p(abs(z) / se), 5)
    return out


def correlate(pairs: pd.DataFrame) -> dict[str, Any]:
    """Between-person and within-person correlation of a paired frame."""
    result: dict[str, Any] = {
        "n_pairs": int(len(pairs)),
        "n_participants": int(pairs["participant"].nunique()) if len(pairs) else 0,
        "n_participant_seasons": int(pairs[["participant", "season"]].drop_duplicates().shape[0]) if len(pairs) else 0,
        "between": {"r": None, "ci": None, "p": None, "n": 0},
        "within": {"r": None, "ci": None, "p": None, "n_pairs": 0, "n_eff": 0,
                   "per_person_median_r": None, "n_persons_used": 0},
        "note": None,
    }
    if len(pairs) < MIN_POINTS:
        result["note"] = f"fewer than {MIN_POINTS} paired days"
        return result

    # Two people always give r = +/-1: a straight line through two points is not
    # a finding, so the between-person estimate needs at least three.
    means = pairs.groupby("participant")[["x", "y"]].mean()
    if len(means) < MIN_PARTICIPANTS_BETWEEN:
        result["between"] = {"r": None, "ci": None, "p": None, "n": int(len(means))}
        result["note"] = f"between-person needs at least {MIN_PARTICIPANTS_BETWEEN} participants"
    else:
        between = _fisher(_pearson(means["x"].to_numpy(), means["y"].to_numpy()), len(means))
        result["between"] = {**between, "n": int(len(means))}

    # person-mean-centred deviations
    dev = pairs.copy()
    dev[["xc", "yc"]] = dev.groupby("participant")[["x", "y"]].transform(lambda s: s - s.mean())
    n_pairs, n_persons = len(dev), dev["participant"].nunique()
    n_eff = n_pairs - n_persons + 1
    within = _fisher(_pearson(dev["xc"].to_numpy(), dev["yc"].to_numpy()), n_eff)
    per_person = [r for _, g in dev.groupby("participant")
                  if len(g) >= MIN_DAYS_PER_PERSON
                  for r in [_pearson(g["x"].to_numpy(), g["y"].to_numpy())] if r is not None]
    result["within"] = {
        **within, "n_pairs": int(n_pairs), "n_eff": int(n_eff),
        "per_person_median_r": round(float(np.median(per_person)), 3) if per_person else None,
        "n_persons_used": len(per_person),
    }
    if n_persons == 1:
        result["note"] = "one participant: the between-person estimate needs more people"
    return result


def fdr(pvalues: list[Optional[float]]) -> list[Optional[float]]:
    """Benjamini-Hochberg q-values, keeping the input order (None passes through)."""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    if not idx:
        return list(pvalues)
    ordered = sorted(idx, key=lambda i: pvalues[i])
    m = len(ordered)
    q: list[Optional[float]] = list(pvalues)
    running = 1.0
    for rank, i in enumerate(reversed(ordered), start=1):
        value = pvalues[i] * m / (m - rank + 1)
        running = min(running, value)
        q[i] = round(min(1.0, running), 5)
    return q


def matrix(long: pd.DataFrame, variables: Iterable[str], lag: str = "same",
           which: str = "within", **filters) -> dict[str, Any]:
    """Every pair of *variables* under one lag, FDR-corrected across the matrix."""
    vars_ = list(dict.fromkeys(variables))
    cells, ps = [], []
    for i, x in enumerate(vars_):
        for y in vars_[i + 1:]:
            pairs = pair(long, x, y, lag, **filters)
            stats = correlate(pairs)
            side = stats[which] if which in ("within", "between") else stats["within"]
            cells.append({
                "x": x, "y": y, "r": side["r"], "ci": side["ci"], "p": side["p"],
                "n_pairs": stats["n_pairs"], "n_participants": stats["n_participants"],
                "note": stats["note"],
            })
            ps.append(side["p"])
    for cell, q in zip(cells, fdr(ps)):
        cell["q"] = q
    return {"variables": vars_, "lag": lag, "which": which, "cells": cells,
            "n_tests": sum(1 for p in ps if p is not None)}


# ---------------------------------------------------------------------------
# overlap / agreement between two measures in the same unit
# ---------------------------------------------------------------------------
#: Units where the difference wraps around the clock.
CIRCULAR_UNITS = {"clock hour"}
#: "Close enough" defaults, per unit.
DEFAULT_TOLERANCE = {"clock hour": 1.0}


def circular_difference(a: float, b: float, period: float = 24.0) -> float:
    """Signed a - b on a circle: 1 - 23 = +2 hours, not -22."""
    half = period / 2.0
    return (a - b + half) % period - half


def agreement(pairs: pd.DataFrame, circular: bool = False, tolerance: float = 1.0,
              period: float = 24.0) -> dict[str, Any]:
    """How closely two same-unit measures land on each other, day by day."""
    out: dict[str, Any] = {
        "n_days": int(len(pairs)), "n_participants": 0, "tolerance": tolerance,
        "circular": bool(circular), "median_difference": None, "median_absolute_difference": None,
        "within_tolerance": None, "pct_within_tolerance": None, "limits_of_agreement": None,
        "per_participant": [], "note": None,
    }
    if pairs.empty:
        out["note"] = "no days where both measures exist"
        return out
    d = pairs.copy()
    d["diff"] = ([circular_difference(x, y, period) for x, y in zip(d["x"], d["y"])]
                 if circular else d["x"] - d["y"])
    diffs = d["diff"].to_numpy(dtype=float)
    within = int(np.sum(np.abs(diffs) <= tolerance))
    out["n_participants"] = int(d["participant"].nunique())
    out["median_difference"] = round(float(np.median(diffs)), 2)
    out["median_absolute_difference"] = round(float(np.median(np.abs(diffs))), 2)
    out["within_tolerance"] = within
    out["pct_within_tolerance"] = round(100.0 * within / len(diffs), 1)
    if len(diffs) > 1:
        mean, sd = float(np.mean(diffs)), float(np.std(diffs, ddof=1))
        # Bland-Altman: where 95 % of day-to-day differences sit.
        out["limits_of_agreement"] = [round(mean - 1.96 * sd, 2), round(mean + 1.96 * sd, 2)]
    for pid, g in d.groupby("display_id"):
        gd = g["diff"].to_numpy(dtype=float)
        out["per_participant"].append({
            "display_id": pid, "n_days": int(len(gd)),
            "median_difference": round(float(np.median(gd)), 2),
            "median_absolute_difference": round(float(np.median(np.abs(gd))), 2),
            "pct_within_tolerance": round(100.0 * float(np.sum(np.abs(gd) <= tolerance)) / len(gd), 1),
        })
    out["per_participant"].sort(key=lambda r: r["display_id"])
    return out


# ---------------------------------------------------------------------------
# comparing groups / seasons / participants (phase P3)
# ---------------------------------------------------------------------------
#: How a comparison splits the days. Seasons use the CALENDAR season the data
#: falls in, because one person's 2nd season can be another's 3rd.
COMPARE_BY = {
    "group": "groups you made",
    "season": "season of the year",
    "participant": "participant",
    "season_n": "each participant's 1st, 2nd... season",
}


def _person_means(d: pd.DataFrame, key: str) -> pd.DataFrame:
    """One value per participant per key: their mean over the included days.

    Person means, not pooled days, so somebody with three weeks of data does
    not outweigh somebody with one.
    """
    return (d.groupby([key, "participant", "display_id"], dropna=False)["value"]
             .agg(["mean", "count"]).reset_index()
             .rename(columns={"mean": "value", "count": "n_days"}))


def _summary_row(key: Any, g: pd.DataFrame, n_days: int) -> dict[str, Any]:
    vals = g["value"].to_numpy(dtype=float)
    n = len(vals)
    mean = float(np.mean(vals)) if n else None
    sd = float(np.std(vals, ddof=1)) if n > 1 else None
    se = sd / math.sqrt(n) if sd is not None and n else None
    return {
        "key": key,
        "n_participants": int(n),
        "n_days": int(n_days),
        "mean": None if mean is None else round(mean, 3),
        "sd": None if sd is None else round(sd, 3),
        "se": None if se is None else round(se, 3),
        "ci": None if se is None else [round(mean - 1.96 * se, 3), round(mean + 1.96 * se, 3)],
        "min": round(float(np.min(vals)), 3) if n else None,
        "max": round(float(np.max(vals)), 3) if n else None,
    }


def _welch(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """Difference of means with a normal-approximation CI and p (no scipy)."""
    na, nb = len(a), len(b)
    out: dict[str, Any] = {"diff": None, "ci": None, "p": None, "n_a": na, "n_b": nb}
    if na < 2 or nb < 2:
        return out
    va, vb = float(np.var(a, ddof=1)), float(np.var(b, ddof=1))
    se = math.sqrt(va / na + vb / nb)
    diff = float(np.mean(a) - np.mean(b))
    out["diff"] = round(diff, 3)
    if se == 0:
        return out
    out["ci"] = [round(diff - 1.96 * se, 3), round(diff + 1.96 * se, 3)]
    out["p"] = round(_norm_p(abs(diff) / se), 5)
    return out


def compare(long: pd.DataFrame, variable: str, by: str = "season",
            groups: Optional[dict[str, list[str]]] = None,
            participants: Optional[list[str]] = None, **filters) -> dict[str, Any]:
    """Summarise one variable split by group / season / participant, and the
    differences between every pair of splits (FDR-corrected)."""
    if by not in COMPARE_BY:
        raise ValueError(f"unknown comparison {by!r}")
    d = day_frame(long, **filters)
    d = d[d["variable"] == variable]
    if participants:
        d = d[d["participant"].isin(list(participants))]
    if d.empty:
        return {"variable": variable, "by": by, "rows": [], "pairs": [], "note": "no days in this range"}

    d = d.copy()
    if by == "group":
        member = {p: name for name, members in (groups or {}).items() for p in members}
        d["_key"] = d["participant"].map(member)
        d = d[d["_key"].notna()]
        if d.empty:
            return {"variable": variable, "by": by, "rows": [], "pairs": [],
                    "note": "none of these participants are in a group yet"}
    elif by == "season":
        d["_key"] = d["season_label"].fillna(d["season"])
    elif by == "season_n":
        d["_key"] = d["season_n"].map(lambda n: f"s{int(n)}" if n == n and n is not None else "?")
    else:
        d["_key"] = d["display_id"]

    per_person = _person_means(d, "_key")
    rows = []
    for key, g in per_person.groupby("_key", dropna=False):
        rows.append(_summary_row(key, g, int(g["n_days"].sum())))
    rows.sort(key=lambda r: str(r["key"]))

    pairs, ps = [], []
    keys = [r["key"] for r in rows]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            va = per_person[per_person["_key"] == a]["value"].to_numpy(dtype=float)
            vb = per_person[per_person["_key"] == b]["value"].to_numpy(dtype=float)
            # paired where the same people appear in both splits (e.g. seasons)
            left = per_person[per_person["_key"] == a].set_index("participant")["value"]
            right = per_person[per_person["_key"] == b].set_index("participant")["value"]
            shared = left.index.intersection(right.index)
            res = _welch(va, vb)
            res.update({"a": a, "b": b, "n_shared_participants": int(len(shared))})
            if len(shared) >= 2:
                diffs = (left.loc[shared] - right.loc[shared]).to_numpy(dtype=float)
                res["paired_diff"] = round(float(np.mean(diffs)), 3)
                sd = float(np.std(diffs, ddof=1))
                se = sd / math.sqrt(len(diffs)) if sd else 0.0
                res["paired_ci"] = ([round(res["paired_diff"] - 1.96 * se, 3),
                                     round(res["paired_diff"] + 1.96 * se, 3)] if se else None)
                res["paired_p"] = round(_norm_p(abs(res["paired_diff"]) / se), 5) if se else None
            pairs.append(res)
            ps.append(res.get("paired_p") if res.get("paired_p") is not None else res["p"])
    for pair, q in zip(pairs, fdr(ps)):
        pair["q"] = q
    return {"variable": variable, "by": by, "by_label": COMPARE_BY[by], "rows": rows,
            "pairs": pairs, "note": None}


# ---------------------------------------------------------------------------
# seasons
# ---------------------------------------------------------------------------
def by_season(long: pd.DataFrame, variables: Optional[Iterable[str]] = None,
              **filters) -> list[dict[str, Any]]:
    """Per variable, a summary for each CALENDAR season the data falls in."""
    d = day_frame(long, **filters)
    if variables:
        d = d[d["variable"].isin(list(variables))]
    if d.empty:
        return []
    from .results import season_type

    rows = []
    for var, g in d.groupby("variable", sort=False):
        for season_lbl, gs in g.groupby(g["season_label"].fillna(g["season"]), dropna=False):
            per_person = gs.groupby("participant")["value"].mean()
            rows.append({
                "variable": var, "device": gs["device"].iloc[0],
                "season": season_lbl,
                "season_ns": sorted({int(n) for n in gs["season_n"].dropna().unique()}),
                "season_types": sorted({season_type(str(s)) for s in gs["season"].unique()}),
                "n_participants": int(gs["participant"].nunique()),
                "n_days": int(len(gs)),
                "mean": round(float(per_person.mean()), 3),
                "sd": round(float(per_person.std(ddof=1)), 3) if len(per_person) > 1 else None,
                "min": round(float(per_person.min()), 3),
                "max": round(float(per_person.max()), 3),
            })
    return rows
