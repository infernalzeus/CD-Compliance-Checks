# CHiP-D measures · what each one is, how it is calculated, and what decides compliance

A tabulated expansion of [`cd-compliance-pipeline.canvas`](cd-compliance-pipeline.canvas).
Every formula and every threshold below was read out of the code, not out of the
papers — the reference is given so the two can be checked against each other.

Thresholds shown are the values live in `config.yaml` on this machine.
A printable version is generated alongside this file as `measures-print.html`.

**Notation.** `a` = the acceleration vector of one raw sample.
`SVM` = the per-epoch signal vector magnitude. `X[d,h]` = the day-by-hour
activity matrix. `X̄` = a grand mean. All logarithms are base 10.

---

## 1 · Step 1 — raw recording to epochs

`actigraphy-epoching` · `.bin` → `<stem>_60s.csv`

| Measure | What it is | Formula | Level / setting | Epoch-length behaviour |
|---|---|---|---|---|
| **SVM_sum** | Movement in one epoch. The per-sample departure of acceleration magnitude from 1 g, summed over the epoch. | `SVM_sum = Σ_samples abs(‖a‖ − 1 g)` | 60 s epochs, GENEActiv reader, no bandpass filter (`--filter no`), `full-summary` mode | **Scales with epoch length.** It is a *sum* over samples (`preprocess/epoch.py:48`), so a 30 s epoch carries about half the value of a 60 s epoch. |
| **Axis SDs** | Per-epoch standard deviation of each axis. Used only for non-wear detection. | `SD(Ax), SD(Ay), SD(Az)` within the epoch | Present only in `full-summary` mode | Mildly affected — a shorter window gives a slightly smaller SD. |

> **The one thing to carry forward:** `SVM_sum` is a sum, not a rate. Anything
> downstream that compares it against an absolute number is epoch-length
> dependent; anything that takes a *ratio* of its variability is not. The
> columns on the right of every table below are an application of exactly that
> rule.

---

## 2 · Compliance — actigraph

`cdcompliance/compliance.py`. This is the orchestrator's own code, not a tool
repo. It is what produces the PASS / REVIEW / FAIL verdict.

| Measure | What it is | Formula | Level | Epoch-length behaviour |
|---|---|---|---|---|
| **Non-wear candidate** | An epoch where the device was essentially motionless. | `(SD(Ax) < τ) + (SD(Ay) < τ) + (SD(Az) < τ) ≥ 2` | `τ = nonwear_axis_sd_g = 0.013 g` (13 mg, van Hees) | Absolute threshold on an SD — **drifts** with epoch length. |
| **Non-wear** | Motionless for long enough that it is removal, not stillness. | a run of consecutive candidate epochs of length `≥ nonwear_min_minutes` | `30 min` | **Breaks.** The code passes *minutes* where the function counts *epochs* (`compliance.py:97`), so at 30 s the rule silently becomes 15 minutes. |
| **Wear fallback** | Used only when the axis SD columns are absent. | `wear = SVM_sum ≥ 5.0` | `activity_threshold_svm = 5.0` | **Breaks** — absolute threshold against a quantity that halves. |
| **Wear hours** | Worn time in a calendar day. | `wear_hours = wear_epochs / epochs_per_hour` | `epochs_per_hour = 60` | **Breaks** — the constant is fixed at 60, so at 30 s every day reads as twice as long. |
| **Diurnal range** | Rejects a flat trace, which is implausible for a worn device. | `max_h mean(SVM in hour h) − min_h mean(SVM in hour h)` | `min_diurnal_range_svm = 20.0` | **Breaks** — absolute threshold against a quantity that halves. |
| **Valid day** | A day that counts towards the verdict. | `wear_hours ≥ 10 AND diurnal_range ≥ 20` | `min_valid_hours = 10.0` | inherits both breaks above |
| **Daily % compliance** | How much of an expected day was worn. | `min(100, wear_hours / expected_wear_hours × 100)` | `expected_wear_hours = 16.0` | inherits the wear-hours break |
| **Verdict** | The participant-season outcome. | `PASS` if `valid_days ≥ 3`; `REVIEW` if `valid_days ≥ 3 − 1`; else `FAIL` | `min_valid_days = 3`, `review_margin_days = 1` | — |

> **Why this table matters for Recompute.** Every threshold in it is absolute,
> and three of the rows are outright wrong at any epoch length other than 60 s.
> This is the reason a recompute variant **does not run compliance at all**: the
> 60 s baseline is the only thing that produces a verdict. See
> the recompute plan (`_local/plans/recompute-and-panel1-plan.md`, local only) § A.

---

## 3 · Step 2 — circadian and sleep metrics

`actigraphy-sleep-metrics` · `<stem>_60s.csv` → non-parametric, daily,
periodogram and SRI CSVs. **Step 2 is not modified by anything in this project.**

### 3.1 The aggregation every measure below sits on

| Step | What happens | Formula |
|---|---|---|
| **Hourly activity** | Each epoch is floored to its calendar hour and averaged. | `X[d,h] = mean(SVM_sum over epochs in hour h of day d)` |
| **Day × hour matrix** | One row per calendar day, 24 columns, `NaN` where there is no recording. | shape `(D, 24)` |

The matrix shape is **the same at any epoch length** — 30 s simply averages 120
epochs per hour instead of 60. But the *level* of `X[d,h]` halves, because
`SVM_sum` is a sum. That single fact determines every right-hand column below.

### 3.2 Non-parametric rhythm measures

| Measure | What it is | Formula | Range | Epoch-length behaviour |
|---|---|---|---|---|
| **IS** · interdaily stability | How repeatable the 24 h pattern is from day to day. 0 = no regularity, 1 = identical every day. | `IS = N·Σ_h(X_h − X̄)² / (P·Σ_i(X_i − X̄)²)`, `P = 24`, `X_h` = column means | 0 – 1 | **Invariant.** A ratio of variances, so the halving cancels. |
| **IV** · intradaily variability | How fragmented the rhythm is within a day. ~0 = smooth, ~2 = noise. | `IV = N·Σ_i(X_{i+1} − X_i)² / ((N−1)·Σ_i(X_i − X̄)²)`, after LOCF gap-fill | 0 – ~2 | **Invariant.** Also a variance ratio. |
| **M10 hour** | Onset of the most active 10 consecutive hours. | circular bidirectional rolling mean of width **11** over `X_h`; take the `argmax` | hour 0 – 23 | **Invariant.** An `argmax` does not care about scale. |
| **M10 level** | Mean activity in that 10 h window. | the rolling-mean value at the `argmax` | activity units | **Halves.** Not comparable across epoch lengths. |
| **L5 hour** | Onset of the least active 5 consecutive hours — the habitual rest window. | circular bidirectional rolling mean of width **6** over `X_h`; take the `argmin` | hour 0 – 23 | **Invariant.** |
| **L5 level** | Mean activity in that 5 h window. | the rolling-mean value at the `argmin` | activity units | **Halves.** |
| **Per-day M10 / L5** | The same two windows, computed one day at a time. | `temp_metrics.compute_daily_metrics` on a **noon-to-noon** window (`start_hour = 12`) so the sleep period sits in the middle | hour + level | hours invariant, levels halve |

> **RA / relative amplitude is not computed.** `RA = (M10 − L5)/(M10 + L5)` and
> the `L5:M10` ratio appear nowhere in step 2 — verified by searching the repo.
> Both are derivable from the columns above, and both would be **invariant**
> (each is a ratio, so the halving cancels). If they are wanted as reported
> measures, that is a small addition to step 2, not a recomputation.

### 3.3 Periodograms — periods 14 to 34 hours

| Measure | What it is | Formula | Epoch-length behaviour |
|---|---|---|---|
| **Enright A_p** | Root-mean-square amplitude of the folded profile at trial period `p`. Large = strong rhythm there. | `A_p = √( (1/P)·Σ_h (X̄_{h,p} − X̄_p)² )` | **Value halves** — it is not variance-normalised. **Peak period is invariant** (`argmax`). |
| **Chi-square Q_p** | Sokolove–Bushell statistic; approximately χ² with `P − 1` df under "no rhythm". | `Q_p = K·N_incl·Σ_h(X_h − X̄)² / Σ_i(X_i − X̄)²`, `K = ⌊N/P⌋`, incomplete cycles dropped | **Invariant** — a variance ratio. So is the p-value and the peak period. |

### 3.4 SRI — sleep regularity index

Computed from the **epoch** series, not the hourly matrix, so it is the one
measure that genuinely depends on epoch resolution.

| Stage | What happens | Formula | Setting |
|---|---|---|---|
| **1 · Normalise** | `SVM_sum` is converted to a per-second rate before scoring. | `activity_t = SVM_sum_t / epoch_sec` | `normalize_per_second = True` (default) |
| **2 · Score sleep/wake** | Cole–Kripke (1992) automatic scoring. | `D_t = scale · Σ_{k=−3..3} w_k · activity_{t+k}`, `w = [106, 54, 58, 76, 230, 74, 67]`; epoch is **sleep** if `D_t < 1`, else **wake** | `scale = 0.001`, `threshold = 1.0` |
| **3 · Align by clock time** | Build a `days × epochs-per-day` matrix. | `M = 86400 / epoch_sec` columns | `epoch_sec` is **inferred from the data** (`cli.py:200`) |
| **4 · Compare consecutive days** | Count epochs that match 24 h apart. | `SRI = 200 · (matching pairs / valid pairs) − 100` | pairs with a missing day excluded |

**Epoch-length behaviour — the precise answer.** Two of the three things you
might worry about are already handled:

- the **amplitude** is handled: stage 1 divides by `epoch_sec`, so a 30 s
  per-second rate is directly comparable to a 60 s one. The halving cancels.
- the **grid** is handled: `epoch_sec` is inferred, so `M` becomes 2880 at 30 s
  and the clock alignment stays correct. There is no silent collision.
- what is **not** handled is the **time span of the kernel**. Cole–Kripke's
  seven weights are defined for 1-minute epochs, so at 60 s the window covers
  7 minutes of real time; at 30 s it covers 3.5 minutes. The scoring therefore
  smooths over half as much real time and flips state more readily, which
  biases SRI **downward**. `sri.py:173` raises a warning whenever
  `epoch_sec ≠ 60`.

So a 30 s SRI is not wildly wrong — it is *differently smoothed*, and the
difference between it and the 60 s value is the kernel, not the participant.
That is why it must not be exported from a variant without the warning
attached.

---

## 4 · MiEYE luminosity

`luminosity-metrics` · one `*-logged.csv` per season. Channel: **melanopic**
(mel EDI / MEDI). Reference thresholds follow Brown et al. 2022.

| Measure | What it is | Formula | Level | Notes |
|---|---|---|---|---|
| **Day mean MEDI** | Mean melanopic lux in the daytime window. | `mean(MEDI for hour ∈ [7, 19))` | `day_window = [7, 19]` | |
| **Night mean MEDI** | Mean melanopic lux in the night window. | `mean(MEDI for hour ∈ [23, 6))` | `night_window = [23, 6]` | wraps midnight |
| **TAT 250** | Minutes per day at or above the daytime adequacy floor. | `count(MEDI ≥ 250 in day window) × epoch_minutes` | `tat_threshold_lx = 250.0` | Brown: ≥ 250 lx recommended by day |
| **TBT 10** | Minutes per night below the "dark enough" ceiling. | `count(MEDI < 10 in night window) × epoch_minutes` | `tbt_threshold_lx = 10.0` | Brown: < 10 lx evening, < 1 lx night |
| **log mean** | Mean of the log-transformed series, since light is log-distributed. | `mean(log₁₀(MEDI + 0.1))` | `eps = 0.1` | |
| **Light hours** | Hours in the day spent above the exposure floor. | `count(MEDI ≥ 10 lx) / epochs_per_hour` | `light_floor_lx = 10.0` | |
| **Day/night ratio** | Rejects a trace with no light rhythm — e.g. a logger left in a drawer. | `day_mean / max(night_mean, 1.0)` | `min_day_night_ratio = 2.0` | night floored at 1 lx to avoid divide-by-zero |
| **Valid day** | A day that counts towards the verdict. | `light_hours ≥ 10 AND ratio ≥ 2` | `min_light_hours = 10.0` | |
| **Daily % compliance** | | `min(100, light_hours / expected_light_hours × 100)` | `expected_light_hours = 10.0` | |
| **Verdict** | | `PASS` if `valid_days ≥ 3`; `REVIEW` if `≥ 2`; else `FAIL` | `min_valid_days = 3`, `review_margin_days = 1` | |
| **M5 / M7 / L2** | Brightest 5 h and 7 h, and darkest 2 h, with onset and mean level. | the light analogue of M10 / L5 | — | `ml_windows.py` |

> **Open issue, carried from earlier sessions:** with `light_floor_lx = 10.0`
> and `min_light_hours = 10.0`, nearly every light day reads as invalid. These
> two thresholds are the ones most in need of calibration against real
> recordings — the rule is working as written, but the written rule looks
> stricter than the data supports.

---

## 5 · ExpiWell ESM

`expiwell-metrics` · one export CSV per season, 4-row preamble.

| Measure | What it is | Formula | Level | Notes |
|---|---|---|---|---|
| **Response rate** | Pooled completion across the scored surveys. | `completed / expected × 100` | — | The export lists **only completed** responses, so `expected` is **configured, never inferred** |
| **Expected denominator** | How many prompts were scheduled. | `expected_days` × the per-survey protocol | `expected_days = 15`, overridable with `schedule_file` | survey types: signal / interval / event |
| **Straight-lining flag** | A response too fast to be genuine. | `duration < 5 s` | `min_plausible_duration_sec = 5.0` | flagged, not excluded |
| **Verdict** | | `PASS` if rate `≥ 70%`; `REVIEW` if `≥ 70 − 10`; else `FAIL` | `min_response_rate_pct = 70.0`, `review_margin_pct = 10.0` | |
| **Affect measures** | Momentary affect items carried into T2. | per-item scores, dated | — | date recovered from the item series where absent |

---

## 6 · T2 — cross-device analysis

`cdcompliance/t2_data.py`, `t2_stats.py`. T2 **reads** what the stages above
produced; it never recomputes a measure. Everything here is a comparison or a
filter.

| Measure | What it is | Formula | Level / guard |
|---|---|---|---|
| **night_of** | The evening a night began, so a diary filled in on the morning of the 5th is attributed to the 4th. | date shifted back for post-midnight records | — |
| **Common window** | The run of days on which **all** selected devices were recording at once. | intersection of each device's date range | flagged `short-common-window` under a week |
| **Clock-hour difference** | Signed difference between two clock hours, respecting the wrap at midnight. | `Δ = ((x − y + 12) mod 24) − 12` | result in `[−12, +12)` |
| **Between-person r** | Correlates each participant's **mean** x against their mean y. Answers "do people who are higher on x tend to be higher on y?" | Pearson over per-participant means | needs `≥ 3` participants, else withheld |
| **Within-person r** | Subtracts each participant's own mean, then correlates the deviations. Answers "on a day when x is higher than usual, is y?" | Pearson over mean-centred deviations, with `n_eff` for the lost degrees of freedom | — |
| **Confidence interval** | | Fisher *z* transform, back-transformed | — |
| **FDR** | Controls false positives across a matrix of correlations. | Benjamini–Hochberg over the p-values | — |
| **Cut-offs** | Exclude days or participant-seasons from a view. | row filter on the long table | **filters only — never recomputes** |
| **`valid` flag** | The device's own valid-day flag, carried through for reference. | from the compliance stage | **T2 does not filter on it** — every day reaching T2 already passed compliance |

---

## 7 · Epoch-length summary

The single table to consult before interpreting a recompute variant.

| Measure | 30 s vs 60 s | Why |
|---|---|---|
| `SVM_sum` per epoch | **halves** | a sum over half as many samples |
| Hourly activity level | **halves** | mean of epoch sums |
| IS, IV | **invariant** | variance ratios |
| M10 / L5 **hour** | **invariant** | `argmax` / `argmin` |
| M10 / L5 **level** | **halves** | an activity level |
| RA, L5:M10 *(not computed)* | would be **invariant** | ratios |
| Chi-square `Q_p`, p-value, peak period | **invariant** | variance ratio |
| Enright `A_p` **value** | **halves** | not variance-normalised |
| Enright **peak period** | **invariant** | `argmax` |
| SRI | **biased downward** | amplitude and grid are handled; the Cole–Kripke kernel spans half the real time |
| Every compliance threshold | **invalid** | all absolute; three rows outright wrong — so compliance is not run on a variant |

**Rule of thumb.** Ratios and positions survive a change of epoch length;
levels and absolute thresholds do not. Of the measures step 2 reports today,
the **timing** results (M10/L5 hours, peak periods) and the **regularity**
results (IS, IV, Q_p) are directly comparable between a 60 s baseline and a
30 s variant. The **levels** are not, and SRI is comparable only with its
warning attached.

---

## Sources in code

| Area | File |
|---|---|
| SVM and epoching | `actigraphy-epoching/preprocess/epoch.py`, `preprocess/io.py`, `cli.py` |
| Actigraph compliance | `cdcompliance/compliance.py`, `cdcompliance/config.py` |
| Hourly aggregation | `actigraphy-sleep-metrics/preprocess.py` |
| IS, IV, M10, L5 | `actigraphy-sleep-metrics/nonparametric.py` |
| Per-day M10/L5 | `actigraphy-sleep-metrics/temp_metrics.py` |
| Periodograms | `actigraphy-sleep-metrics/periodogram.py` |
| SRI | `actigraphy-sleep-metrics/sri.py` |
| Luminosity | `luminosity-metrics/metrics.py`, `compliance.py`, `ml_windows.py` |
| ExpiWell | `expiwell-metrics/`, `cdcompliance/devices/expiwell.py` |
| T2 | `cdcompliance/t2_data.py`, `t2_stats.py`, `t2_export.py` |
