# T2 — cross-device dataset preview and export (plan)

Status: **P0-P5 built** (2026-09-18) - data layer and dictionary, the
Visualise -> T2 hand-off, and a T2 preview with coverage, timelines,
cross-device correlations, season summaries and group/season comparisons.
What-if thresholds (P4) and Export (P5) are next.

## Purpose and flow

T2 is a **preview**: it shows how a cross-device dataset would look before anything
is produced. Nothing is processed or written until **Export** is pressed.

```
Visualise                     T2 (preview only)                    Export
select + user checks  ──►  visualise the would-be dataset  ──►  approve, freeze, write
(incl. 60 s files exist)    sliders · groups · compare · what-if    manifest + downloads
```

- **Visualise → T2:** the user selects participants, seasons, devices and
  variables, does their own checks (including that the Actigraph 60 s outputs
  exist), and sends the selection to T2. No files are written.
- **T2:** reads existing outputs to draw the preview. It runs no analysis tools
  and writes nothing. Sliders, groups, comparisons and threshold what-ifs all
  change only the preview.
- **Export:** the one step that produces anything. It records the approver,
  freezes the dataset exactly as previewed, writes it with a manifest, and offers
  the downloads.

## Decisions

| Topic | Decision |
|---|---|
| Where approval happens | At **T2 → Export**, not at Visualise → T2 |
| Storage | Exports go to the per-user `t2_root` (⚙), default `N:\CSI\Lab Files\CHiP - D\T2 Output Test` |
| Granularity | Day level (participant + date) and season summaries |
| Night key | A night is identified by the **evening it starts** |
| Seasons | Comparisons key on the **calendar season the recordings fall in** (Autumn 2025), not the folder label or the visit number |
| Expiwell alignment | Actual response dates (see below); day of study kept as a label |
| Inclusion | Rule chosen before export and recorded |
| Groups | **Created by the user in T2** (e.g. Group A / Group B from selected participants). No demographics are used |
| Comparisons | Groups · seasons · different T2 selections · same data under different thresholds |
| What-if thresholds | Actigraph wear · MiEYE light · Expiwell response rate · analysis cut-offs |
| Sliders | Date range (by week), thresholds, metric cut-offs |
| Non-wear / 60 s files | The 60 s outputs must exist; this is a user check on the Visualise panel (details to follow) |
| Downloads | ZIP (tidy CSV + dictionary + manifest) · wide CSV (one row per participant-day) · charts PDF |

## Aligning devices in time

Checked against real study data (one participant-season, illustrative dates):

| Device | Dates covered |
|---|---|
| Expiwell (day of study 1–15) | day 1 → day 19 — **19 calendar days** |
| MiEYE | day 1 → day 28 |
| Actigraph | day 4, 12:00 → day 25, 11:59 |

- **Day of study is not a calendar count.** Days 1–15 spanned 19 calendar days,
  and day N matched "start date + N − 1" on only 1 of 15 days (the same held for
  the other participant-seasons checked). Placing responses by day of study would misdate them by up to 4 days.
- **Every Expiwell response carries its actual timestamp** (234/234 checked), so
  responses are placed on their **real date**; day of study stays as a label.
  Fallback, only if a timestamp were missing: map day N to that participant's
  N-th response date.
- **Devices start on different days** (Actigraph began 3 days after Expiwell and
  MiEYE in the example). The preview shows each device's window and the **common window**
  where all selected devices overlap (days 4–19 in the example); the date slider defaults
  to that window.
- Actigraph recordings run noon to noon, so the first and last days are partial;
  they are marked and excluded by default.

## 1. Data dictionary

`cdcompliance/t2_dictionary.yaml` — every selectable variable: id, device, level
(day / season), unit, definition, source (output file + column). It drives the
Visualise selector and ships with every export. Output columns not in the
dictionary are listed so nothing new goes unnoticed.

| Device | Day level | Season level |
|---|---|---|
| Actigraph | M10/L5 activity + onset hour, wear hours, valid day, % compliance | IS, IV, M10/L5, SRI, valid days, verdict |
| MiEYE | melanopic mean/median/max, geometric mean, day/night mean, TAT (≥250 lx), TBT, M5/M7/L2 onset + level, light hours, valid day | means, verdict |
| Expiwell | sleep diary TST/SOL/WASO/SE/TIB/awakenings/quality/restedness; positive/negative affect; sleepiness (KSS) | response rate, item means, verdict |

Internally everything is one long table:
`participant, season, date, night_of, day_of_study, device, variable, value, level, valid`

## 2. T2 panel

- **Selections:** the selections sent from Visualise, each previewable.
- **Sliders:** date range by week (within the common window), threshold sliders
  (re-apply compliance live in the preview), metric cut-offs (e.g. short sleep,
  sleep efficiency).
- **Groups:** build named groups from the selected participants, then compare.
- **Preview views:**
  - **Cross-device (core):** variables from different devices with a lag (same
    day · that night · next day) → scatter with within-person and between-person
    correlations; correlation matrix; paired days counted.
  - **Timelines:** one participant, all chosen variables on a shared date axis.
  - **Seasons:** each variable across Autumn / Winter / Spring / Summer.
  - **Compare:** groups, seasons, or two selections side by side.
  - **Threshold what-if:** original vs adjusted — who and which days enter or
    leave, and how each result shifts.
- **Export** button.

## 3. Export (the only step that writes)

1. Choose the inclusion rule (PASS / PASS + REVIEW / all with verdict column);
   the current slider, group and threshold settings are taken as they are.
2. Enter the approver.
3. Write, then offer the downloads:

```
<t2_root>/<id>_<name>/
    data_long.csv          tidy, one row per participant/date/variable
    data_wide_day.csv      one row per participant-day
    season_summary.csv
    dictionary.csv         variables used, with definitions
    manifest.json          provenance
    report.pdf             the charts, as previewed
```

`manifest.json`: id, name, approver and time, the selection, groups, date range,
inclusion rule, **every threshold in force** (and which were changed from the
defaults), each source output file with SHA-256 and modified time, each tool
repo's git commit, naming flags present, and row counts.

Exports are **immutable**. Exporting again after changing sliders creates a new
export rather than editing the old one. Because `t2_root` may be shared, ids
combine timestamp + account, and files are written to a temporary folder then
renamed into place.

## 4. Statistical guard-rails

- Days repeat within each person: report **within-person** (person-mean-centred)
  and **between-person** associations separately; never pool naively.
- Always show n (participants and paired days) beside every estimate.
- Correlation matrices test many pairs: show confidence intervals and flag
  multiple comparisons (FDR-adjusted p).
- What-if results are exploratory; an export records the thresholds it used.

## Phases

| Phase | Delivers |
|---|---|
| P0 | **done** - Data dictionary; real-date alignment for Expiwell; night key; common-window logic; date added to the Expiwell affect output |
| P1 | **done** - Visualise → T2 hand-off (with the 60 s check); T2 preview with week slider and timelines |
| P2 | **done** - cross-device views (lags, within/between correlations, FDR matrix), seasons |
| P3 | **done** - groups made in T2, and comparisons by group / calendar season / participant |
| P4 | **done** - cut-off sliders over the numbers the tools already produced (no reprocessing) |
| P5 | **done** - export with a file list shown first, approver, manifest, provenance, device reports and a charts PDF |

## P0 - what exists

| Piece | Where | Notes |
|---|---|---|
| Data dictionary | `cdcompliance/t2_dictionary.yaml` | 42 variables (Actigraph 15, MiEYE 16, Expiwell 11); sources, timing, valid flags. Sleepiness (KSS) isn't in any output yet |
| Long table + windows | `cdcompliance/t2_data.py` | read-only; see its docstring for the columns |
| Real dates | Expiwell affect output now has `timestamp`/`date`; older outputs fall back to the item series |
| Night key | `timing` per variable: `noon_to_noon` (Actigraph M10/L5 row D covers noon D to noon D+1, so night_of = D), `diary_morning` (night_of = D-1), `onset_hour` (MiEYE L2) |
| Partial days | calendar days under 90 % of epochs; noon windows need both halves |
| Common window | fully recorded **calendar** days of every selected device overlap |
| Season numbers | `season_n` 1..n per participant, ordered by the **median date of the data** (then dates in filenames, then folder name); folder labels disagree across participants |
| Batch ids | `PRE-…` preprocess (no season), `T2-…-s24` (seasonal) |
| Pseudonymous IDs | `cdcompliance/pseudo_id.py`, `privacy.pseudonymise` **off** |
| API (read-only) | `GET /api/t2/dictionary`, `GET /api/privacy`, `GET /api/participant/{pid}/seasons`, `POST /api/t2/availability` |
| Tests | `python -m unittest tests.test_t2_p0` (synthetic data only) |

Availability flags (Visualise to T2 check, user may still continue):
`season-not-in-master`, `season-number-absent`, `device-output-missing`,
`actigraph-60s-missing`, `source-missing`, `column-missing`, `device-no-days`,
`no-common-window` (missing); `short-common-window`, `season-order-estimated`,
`affect-date-from-series` (info).

## P1 - what exists

| Piece | Where |
|---|---|
| **SEND TO T2** dialog: seasons (with participant counts), devices, variables, availability check, required user checks, initials | Visualise panel |
| Availability check: device spans, common window, missing devices / 60 s files / source files / overlaps; user may continue anyway | `t2_selections.summary` |
| Selection record `T2-<ts>-<initials>-s<seasons>`: public stripped + private detail (participants, name, checks, availability at send) | `cdcompliance/t2_selections.py` |
| T2 panel: selection list with archive, coverage rows, week-range slider (remembered per selection), timelines (max 8 charts, valid / invalid / partial days, shaded window), season-level values | `webapp/static/app.js` |
| API | `POST /api/t2/seasons`, `POST /api/t2/availability`, `POST|GET /api/t2/selections`, `GET /api/t2/selections/{id}`, `POST /api/t2/selections/{id}/archive`, `POST /api/t2/timeline` |
| Tests | `tests/test_t2_p0.py` (15 tests, synthetic data; asserts master is byte-identical after sending and previewing) |

Answers to the earlier open questions:

1. **Selections are remembered** - as records in `runs/` (definitions only, no
   data), so they survive restarts and the list shows who sent what and when.
2. **Visualise checks before sending:** the Actigraph 60 s outputs exist (the
   dialog also checks and reports this) and the compliance verdicts have been
   reviewed. Both are recorded in the private record. `t2_selections.USER_CHECKS`
   is where more checks get added.

## P2 - what exists

| Piece | Where |
|---|---|
| Pairing with lags (`same`, `night`, `next`), honouring the week slider and the valid / partial filters | `cdcompliance/t2_stats.py` |
| Within-person (person-mean-centred, effective n = pairs - people + 1) and between-person (person means) correlations, each with a Fisher-z 95 % CI and p; plus the median per-person r | `t2_stats.correlate` |
| Correlation matrix over up to 12 variables, Benjamini-Hochberg q across the matrix | `t2_stats.matrix` |
| Season summaries as means of participant means | `t2_stats.by_season` |
| T2 views: Coverage / Cross-device / Seasons; scatter coloured per participant with person means, centring toggle, clickable matrix | `webapp/static/app.js` |
| API | `POST /api/t2/scatter`, `POST /api/t2/matrix`, `POST /api/t2/seasons-summary` |
| Tests | `tests/test_t2_stats.py` (12 tests), including a constructed case where within-person r = +1 while between-person r = -1 |

Notes and limits:

- p-values and CIs use the Fisher z transform with a **normal approximation**
  (scipy is not a dashboard dependency). Fine for an exploratory preview; an
  export records that these are exploratory.
- A perfect or zero-variance relationship gets an r (or a dash) but no CI or p,
  rather than an infinite one.
- Within-person centring is by **participant**, so a participant with two
  seasons contributes their across-season variation to the within-person
  estimate. If that turns out to matter, centring per participant-season is a
  one-line change (`t2_stats`).
- The preview caches a selection's long table for two minutes so the scatter
  and matrix don't re-read every output file on each click.

## P3 - what exists

| Piece | Where |
|---|---|
| Calendar season identity from the recording dates: `season_key` (`2025-autumn`), label (`Winter 2025/26`), code (`aut25`) | `t2_data.calendar_season` |
| Season chips, availability and T2 records all keyed on the calendar season; T2 batch id carries season codes (`T2-...-YK-aut25`) | `t2_selections`, `batches.season_tag` |
| Groups made by hand in T2 (no demographics), saved in the **private** record only | `t2_selections.set_groups` |
| Compare one measure by group / calendar season / participant / visit number: person means, 95 % CI, pairwise differences (paired when the same people are on both sides), FDR-corrected | `t2_stats.compare` |
| Day-level range slider (not just weeks) and the valid / part-day filters, applying to every view | `webapp/static/app.js` |
| API | `POST /api/t2/compare`, `GET|POST /api/t2/selections/{id}/groups` |

Why calendar seasons: one participant's 2nd season can be another's 3rd, and
the folder labels disagree - CD008's `Winter 2025` and CD011's `Winter 2026`
both hold January 2026 data. The visit number (`s1`, `s2`...) is kept for one
participant's own progression through the year.

Guard-rails added with P3:

- A between-person correlation needs **3+ participants** (a line through two
  points is not a finding); it reports a dash and says so.
- A difference between two splits needs 2+ people on each side.
- Comparisons average **person means**, so somebody with three weeks of data
  does not outweigh somebody with one.

## What a comparison in T2 actually means

Everything in T2 has already passed the Visualise panel, so T2 does **not**
re-check compliance: every recorded day is included. The only day filter left is
*part-days* (a first or last day that was only half recorded), which is about
coverage, not compliance.

Measures arrive on very different scales - clock hours, minutes, melanopic lux,
1-7 ratings - so the question decides the tool:

| Question | View | What it does | Valid when |
|---|---|---|---|
| What data is there? | **Coverage** | days per device, the overlap window | always |
| Are these two the *same*? e.g. is the darkest 2 h the same hour as the least-active 5 h | **Agreement** | day-by-day difference, % within a tolerance, typical gap, day-to-day spread | both measures share a **unit**; clock hours are treated circularly (23:00 vs 01:00 = 2 h) |
| Does one measure *move with* another? | **Relationships** | correlation, split into within-person and between-person | any two measures - correlation is scale-free; needs 3+ people for the between-person half |
| Is this measure *different* between groups / seasons / people? | **Compare** | mean of participant means, 95 % CI, differences (paired when the same people are on both sides) | one measure at a time, in its own unit |

Deliberately **not** offered, because they would be misleading:

- correlating two measures and calling it agreement (a perfect correlation can
  sit hours away from the identity line - `light_l2_onset` can track
  `act_l5_hour` exactly while always being 2 h earlier);
- overlapping measures in different units (there is no meaningful "gap" between
  melanopic lux and minutes);
- pooling repeated days as if they were independent people;
- a between-person correlation from fewer than three people.

Seasons are compared as **seasons of the year** (Compare -> "season of the
year"); a participant's own progression is Compare -> "1st / 2nd / 3rd season".
The old separate Seasons view was the same thing twice, so it is gone.

Day ranges are labelled by the days they cover ("days 1-7", "days 8-15"). An
earlier build offered "week 1/2/3" for a 15-day window, which implied three
weeks of data when the third was a single day.

## Season paperwork (assessment log)

Each staging season folder may hold an assessment log
(`CDxxx-<Month>-<YY>-Assessment-Log-S<n>.xlsx`; 134 of 451 season folders have
one). The compliance run now copies it verbatim into the matching output season
folder - no parsing, no dependency on it:

    <output>/<CDxxx>/<Season>/CDxxx-September-25-Assessment-Log-S1 .xlsx

It contains the visit number, the confirmed bed/wake times, device start dates,
and a work-schedule sheet mapping **Day 1-15 ESM to real dates** with the shift
type for each day. For CD008 Autumn 2025 that mapping (28 Sep - 12 Oct) matches
the common window T2 derives from the data independently.

Not used yet (agreed: decide later): duration cut-offs at Visualise -> T2, and
the shift/rest-day column as a day-level grouping.

## Which panel answers which question

| Panel | Compares | Answers |
|---|---|---|
| **Coverage** | one participant-season per row, every device | what data exists, and where the devices overlap |
| **Agreement** | one participant at a time, two measures sharing a unit | do they land on the same value (e.g. darkest 2 h vs least-active 5 h) |
| **Relationships** | all participants pooled, split within-person / between-person | does one measure move with another, across different scales |
| **Compare** | all participants, one measure | is it different across seasons of the year, groups or people |

Compare is where **cross-season** lives: "season of the year" puts Autumn 2025
beside Winter 2025/26 using the same people on both sides where possible, and
"1st / 2nd / 3rd season" follows one participant's own progression.

## P4 - cut-offs (no reprocessing)

A cut-off keeps days whose value for one measure meets a rule
(`act_wear_hours >= 22`). It is applied to numbers the tools have already
produced, so **nothing is reprocessed**: it is a filter over the preview, and it
carries through every panel and into the export. Cut-offs stack, and a day with
no value for that measure cannot be judged, so it drops out.

What a slider here **cannot** change, because the value itself was computed with
it (`t2_selections.BAKED_IN_THRESHOLDS`, listed in the UI):

| Threshold | Changes | Lives in |
|---|---|---|
| light floor (melanopic lx) | `light_hours`, `light_pct_compliance` | luminosity-metrics |
| TAT / TBT levels | `light_tat_min`, `light_tbt_min` | luminosity-metrics |
| day / night windows | `light_day_mean`, `light_night_mean` | luminosity-metrics |
| non-wear rule | `act_wear_hours`, `act_nonwear_hours` | the compliance step |
| epoch length (60 s) | every Actigraph measure | actigraphy-epoching |
| expected ESM prompts | `esm_response_rate` | expiwell-metrics |

Moving one of those means re-running the device tools from the raw data - a
Compliance-panel run, not a T2 slider.

## P5 - export

The Export dialog lists **what would be written before anything is written**:
each file with its row and column counts and a plain description, the
destination folder, and the inclusion summary (seasons, devices, day range,
part-days, cut-offs). Writing needs the approver's initials.

    <t2_root>/T2-<yyyymmdd-HHMMSS>-<initials>-<seasons>/
        data_long.csv  data_wide_day.csv  season_summary.csv
        dictionary.csv  manifest.json  README.txt
        report.pdf                      the charts, as previewed
        reports/<participant>/<season>/*.pdf

The folder carries **one** timestamp - when the export happened. The selection
it came from is in `manifest.json` (`selection_id`), not repeated in the name.

`reports/` holds the device report PDFs the tools produced, for the
participant-seasons in this export only. They are skipped entirely while
pseudonymous IDs are on, because their filenames carry the real participant ID.

Files are written to a hidden folder beside the destination and renamed into
place, so a half-written export never appears on a shared drive. Exports are
never edited; exporting again makes a new folder. `manifest.json` records the
approver and time, the inclusion, every threshold in force, each tool's git
commit, and every source output file with its SHA-256.

`report.pdf` holds the charts: a cover with the inclusion, what each device
recorded and when, each measure by season (or, when only one season is
exported, participant by participant), a day-by-day page per participant-season
(capped at 12 pages), and a page for clock-hour measures that should agree.
Panels are filled device by device, so one page covers the whole study rather
than nine Actigraph measures.

matplotlib is now in `requirements.txt`. Where it is missing the export still
runs: `report.pdf` is skipped, the preview says so, and the manifest records it.

## Naming

The panel was called *Overlap*, but Coverage already uses "overlap" for the
window where every device is recording at once. Two meanings of one word in one
panel is a bug, so the panel is now **Agreement**. Coverage, Relationships and
Compare say what they do and are unchanged.

## Open questions

1. **Winter labelling (yours to decide):** the folder labels disagree across
   participants for the same calendar winter. T2 ignores the labels and uses
   the recording dates; the compliance side still shows the folder name.
2. **Anything else to confirm before sending?** Today it is the 60 s outputs and
   the verdicts; more checks can be added in `t2_selections.USER_CHECKS`.
