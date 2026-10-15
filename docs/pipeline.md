# CHiP-D Compliance — System Architecture

> **The flowchart lives in [`cd-compliance-pipeline.canvas`](cd-compliance-pipeline.canvas)**
> (open in Obsidian). It reads left to right: raw data from SharePoint → one lane
> per device (what each score means and how it is calculated) → the master tree →
> the dashboard (Compliance → Visualise → T2 → Export). This page is the text
> companion to that canvas.

How the whole pipeline fits together: from the OneDrive staging tree, through the
CD-Compliance-Checks orchestrator and its external device tools, into the
dashboard output tree and the three-tab web UI (**Compliance · Visualise · T2**).

The core is **UI-agnostic** and **device-agnostic**: the CLI (`run.py`) and the
web dashboard (`serve.py`) drive the same engine (`cdcompliance/pipeline.py`),
and each device is a self-contained processor behind one small interface
(`cdcompliance/devices/base.py`). Adding a device = one new `devices/<name>.py`
+ one registry line.

## External tool repos

The analysis code lives in **standalone script repos**, not in this project. They
are not pip packages (loose `cli.py` files with relative imports, each with its
own `numpy/pandas/...`), so `requirements.txt` can't install them. Instead
`setup_tools.py` **clones them from GitHub** into the folders named in
`config.yaml → tools`, and installs each one's own `requirements.txt`:

| Step | Repo | Role |
|------|------|------|
| Step 1 | `actigraphy-epoching` | `.bin` → 60-second epoch CSV (SVM) |
| Step 2 | `actigraphy-sleep-metrics` | epoch CSV → IS/IV/M10·L5/SRI + PDF |
| MiEYE | `luminosity-metrics` | light CSV → metrics + compliance + PDF |
| Expiwell | `expiwell-metrics` | survey CSVs → response rates, diary + affect measures, PDF |

## Appendix · `actigraphy-calibration` — a separate project

**Not a step in this pipeline, and not on the canvas.** It is its own private
repo exploring whether accelerometer calibration changes the measures; nothing
here clones, configures or calls it, and no decision has been taken to adopt it.
It is recorded here only so the question "what would it change?" has an answer.

It does not reimplement any measure — it re-runs **step 2's own functions** on a
corrected epoch series, so the only difference is the input. Three corrections,
mathematically different animals:

| Stage | Transformation | Character |
|---|---|---|
| **1 · device autocalibration** | per-axis scale `s` and offset `o` fitted by sphere-fitting still epochs (van Hees 2014), then applied per epoch: `SVM' = SVM − n·\|‖ā‖−1\| + n·\|‖(ā−o)s‖−1\|` | **not** a global affine map — the amount subtracted depends on each epoch's own orientation |
| **2 · residual floor** | subtract the 1st percentile of the corrected series, clip at 0 | affine **plus a clip**, so it is non-linear at the bottom of the distribution |
| **3 · position gain** | multiply by `b = M10_ref / M10_pos` (default `m10`; `p95` and `theilsen` also offered) | a true global multiply |

Numbers below are the DWJ calibration recording (one participant, wrist + ankle +
upper arm worn simultaneously, 8 days), as `outputs/calibration_metrics.csv`
reports them — ankle / upper-arm / wrist.

| Measure | What step 2 computes | What calibration does to it | raw → device-calibrated → position-calibrated |
|---|---|---|---|
| **M10 onset hour** | clock hour of the most active 10 h in a noon-to-noon window | nothing in practice | 15 / 15 / 15 → unchanged → unchanged |
| **L5 onset hour** | clock hour of the least active 5 h | **can move**: the upper arm shifted by an hour under stage 1 | 3 / 3 / 2 → 3 / **2** / 2 → unchanged |
| **M10 activity level** | mean activity in that window | shifts (stage 1–2) **and** scales (stage 3) | 205 / 166 / 243 → 152 / 154 / 227 → 207 / 224 / 227 |
| **L5 activity level** | mean activity in the rest window | the big one: nearly half of the ankle's "rest" was sensor error | 141 / 109 / 132 → 73 / 69 / 97 → 100 / 101 / 97 |
| **L5 / M10** | rest-to-active ratio | shifts with the offset, **immune** to gain | 0.69 / 0.65 / 0.54 → 0.48 / 0.45 / 0.43 → identical |
| **RA** (M10−L5)/(M10+L5) | rhythm amplitude | same: offset moves it, gain cannot | 0.18 / 0.21 / 0.30 → 0.35 / 0.38 / 0.40 → identical |
| **IS** interdaily stability | day-to-day repeatability of the 24 h profile | **moves** (+8 to +18 %), because stage 1 is per-epoch, not a single affine map | 0.29 / 0.42 / 0.50 → 0.32 / 0.50 / 0.54 → identical |
| **IV** intradaily variability | hour-to-hour fragmentation | **moves** (−7 to −19 %) for the same reason | 1.68 / 1.13 / 0.82 → 1.57 / 0.92 / 0.77 → identical |
| **SRI** | % of minutes in the same sleep/wake state 24 h apart (Cole–Kripke) | **not evaluated by the calibration project at all** — yet it thresholds `SVM/epoch_sec`, so it is *not* scale-invariant and calibration would change it. Gap. | — |
| **Periodogram** | Enright / χ² over 14–34 h | not evaluated; variance-ratio form suggests it behaves like IS | — |

#### Two things worth reviewing

**The invariance claim is weaker than the README states.** `cli.py sensitivity`
applies a *single global* `SVM → (SVM − a)·b` and correctly shows IS, IV and both
onset hours unmoved. But the correction the tool actually applies is per-epoch and
clipped, so in the same run IS moves up to +18 %, IV up to −19 %, and one L5 onset
hour shifts. Only stage 3 (position gain) is a genuine global multiply — and there
the invariance holds exactly, which is why the device→position columns above are
identical for IS, IV, RA and L5/M10.

**Device calibration alone makes cross-position *amplitude* agreement worse.**
Spread across the three positions (CV %), from `calibration_spread.csv`:

| | raw | device-calibrated | position-calibrated |
|---|---|---|---|
| M10 level | 37.4 % | **42.5 %** | 9.3 % |
| L5 level | 25.5 % | **34.1 %** | 4.6 % |
| RA | 49.1 % | 14.2 % | 14.2 % |
| L5/M10 | 23.3 % | 12.5 % | 12.5 % |

So the headline "3.5× better agreement" is true of the *ratios*, while the
*levels* get slightly worse until the position gain is applied. Both stages are
needed for amplitudes; ratios only need stage 1.

#### Where it sits


`Collab Projects\actigraphy-calibration` (private) is a **separate project** that
re-runs Step 2's own functions on an affine-corrected epoch series
(`SVM → (SVM − a) · b`) so **amplitude** measures (M10/L5 activity level, RA)
are comparable across body positions. It is **not cloned, configured or called by this
orchestrator, and not on the canvas**: it is absent from `setup_tools.py`, `config.yaml → tools`
and `cdcompliance/updates.py`. Its own recommendation is to fold autocalibration
upstream into Step 1 rather than keep it as a separate pass, because an
epoch-level correction is an approximation of a per-sample one. Timing and
regularity measures (M10/L5 *hour*, IS, IV) are invariant to that correction and
need no calibration at all.

```bash
python -m pip install -r requirements.txt   # orchestrator + dashboard deps
python setup_tools.py                        # clone the 3 tool repos + their deps
python setup_tools.py --update               # git pull them next time
```

Compliance **thresholds** live in this project's `config.yaml` (`compliance:` for
actigraph, `luminosity:` for MiEYE). Actigraph's compliance *logic* lives here
too; MiEYE's compliance logic lives in `luminosity-metrics` (so that repo is
self-sufficient standalone) and this project passes its thresholds in as CLI
flags and reads the tool's `*_compliance.json` back.

## Whole-system flowchart

Open **[`cd-compliance-pipeline.canvas`](cd-compliance-pipeline.canvas)** in Obsidian
for the visual flowchart. At a glance:

- **OneDrive staging (read-only)** → `Actigraph/*.bin` and `MiEYE/*-logged.csv`.
- **Orchestrator** (`cdcompliance/`): `config.py` → `discovery.py` → `devices/registry.py`
  → per-device processor → `pipeline.py`, with `onedrive.py` hydration and an
  `events.py` EventBus streaming to console / JSONL / WebSocket. Both `run.py` (CLI)
  and `serve.py` (web) drive the same engine.
- **External tool repos** (cloned by `setup_tools.py`): `actigraphy-epoching` (Step 1),
  `actigraphy-sleep-metrics` (Step 2), `luminosity-metrics` (MiEYE), `expiwell-metrics` (ESM).
- **Output tree** `DASHBOARD TEST Output/CDxxx/Season/<Device>/` → read back by the
  device-agnostic **Visualise** panel, and on demand by **T2**.
- **Runs** `runs/batches/*.json` (public, stripped) + `runs/_private/` (local detail).

## Per-device sub-flows

### Actigraph (`devices/actigraph.py`)
1. **Hydrate** the `.bin` from OneDrive (live download %), unless the `_60s.csv`
   already exists (resume — skips the ~1 GB download + epoching).
2. **Step 1** `actigraphy-epoching`: `.bin` → `<stem>_60s.csv` (+ metadata) and a
   Step 1 sleep-report PDF.
3. **Compliance** (`cdcompliance/compliance.py`): non-wear detection (axis-SD, van
   Hees) → valid-day wear rule → verdict; writes `<stem>_daily_compliance.csv`.
4. **Step 2** `actigraphy-sleep-metrics`: circadian/sleep metrics CSVs + PDF,
   copied into the output folder.
5. **Excel** per-item workbook + `<stem>_compliance.json`; optional raw `.bin`
   replica.

### MiEYE (`devices/mieye.py`)
1. **Hydrate** the one `*-logged.csv` from OneDrive (only that sheet is read).
2. **luminosity-metrics** (`cli.py`, cwd = the tool repo): reads the sheet →
   heatmaps + histograms + **per-day light profiles** + **per-day distributions**
   PDF; computes per-day light-adequacy metrics and the **wear-based valid-day**
   compliance verdict; writes `<stem>_luminosity_report.pdf`,
   `<stem>_luminosity_metrics.csv`, `<stem>_daily_compliance.csv`, and
   `<stem>_compliance.json` straight into the output folder.
3. The device **relays** that `_compliance.json` into an `ItemResult` for the run
   summary + participant roll-up. Thresholds come from `config.yaml → luminosity:`.

## Dashboard (device-agnostic)

`webapp/server.py` + `cdcompliance/results.py` read the output tree generically:
`output_items()` globs every `*_compliance.json` and reads its `verdict` +
`summary`, so any device that writes a standard compliance JSON rolls up into
Panel 2's aggregate automatically. The per-item drill-down (`item_measures`)
lists that device's inspectable CSV/PDF outputs.

> **See also:** `docs/cd-compliance-pipeline.canvas` (Obsidian canvas view) and the
> per-repo READMEs. The luminosity tool's own docs live in
> [`luminosity-metrics/README.md`](https://github.com/infernalzeus/luminosity-metrics).

## Before a run: the naming check

`naming.py` + `naming_rules.yaml` check the staging filenames against the
convention *before* anything is processed, mirroring discovery so the flags mean
what the pipeline will actually do: **✕ skipped** (present but unusable),
**! warning** (processed, but only through a fallback or with a mismatched ID),
**i info** (drifted but recognised). Flags show as grid badges, a ⚑ filter, and
an expected-vs-current list per folder.

## After a run: batches and season paperwork

Every run that writes is one **batch**, recorded twice: `runs/batches/<id>.json`
is public and deliberately stripped (initials, time, mode, counts, thresholds,
tool commits — `assert_public_safe` refuses participant IDs, paths, filenames,
serials and dates), while `runs/_private/<id>/` keeps the full detail and is
git-ignored. Ids are `PRE-<ts>-<initials>` for preprocessing (never seasonal, one
run can span seasons) and `T2-<ts>-<initials>-<seasons>` for the T2 stage.

`pipeline.copy_season_documents` also copies each season's **assessment log**
from staging into the matching output season folder, verbatim. It records the
visit number, device start dates, confirmed bed/wake times and a work schedule
that maps *Day 1–15 ESM* to real dates — useful context that travels with the
results. Nothing parses it yet.

## Pipe 5 · T2 (Visualise → T2 → Export)

T2 builds the cross-device dataset. It **reads only**; the single step that
writes is Export.

1. **Send** (Visualise): choose seasons — *seasons of the year*, derived from the
   recording dates rather than the folder label, because one participant's `s1`
   is another's `s2` and the labels disagree — plus devices and measures. The
   availability check reports each device's days, the **common window** where the
   chosen devices overlap, and any gaps. Two user checks and initials are
   required; the record written contains no data.
2. **Preview**: one long table (participant · season · date · `night_of` · device
   · measure · value · part-day · window day) behind four views — **Coverage**,
   **Agreement** (same-unit, clock hours compared circularly), **Relationships**
   (within-person vs between-person correlation, FDR-corrected), **Compare** (by
   season of the year, by group, or by person). Everything here already passed
   compliance, so T2 does not re-judge it.
3. **Cut-offs**: keep days whose measure passes a rule. Applied to the numbers the
   tools already produced, so nothing is recomputed. Thresholds baked *into* those
   numbers (light floor, non-wear rule, epoch length…) can only change by
   re-running the device tools — a **Recompute** step that is a placeholder today.
4. **Export**: lists every file first, then needs the approver's initials and
   writes `data_long.csv`, `data_wide_day.csv`, `season_summary.csv`,
   `dictionary.csv`, `report.pdf`, `reports/` (the device PDFs) and
   `manifest.json` (approver, inclusion, thresholds, tool commits, SHA-256 of
   every input) into `<t2_root>/T2-<ts>-<initials>-<seasons>/`.

Full phase notes: [`t2-plan.md`](t2-plan.md).
