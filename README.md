# CD-Compliance-Checks

Compliance-check pipeline for the **Wellcome Trust CHiP-D** data repository.

It reads a participant's OneDrive-synced data, runs each supported device's
quality/compliance checks, and replicates the checked, processed outputs into a
dashboard output tree. The core is UI-agnostic: the CLI here and a future web UI
drive the same engine and consume the same structured progress event stream.

**Implemented today:** Actigraph (GENEActiv `.bin`).
**Stubbed for later:** Expiwell, Saliva, Cognitron, Qualtrics, MiEye.

---

## What it does (Actigraph)

For each season under a participant, for each `.bin` in that season's
`Actigraph` folder:

1. **Download from OneDrive** — the `.bin` (~1.1 GB) is usually a dehydrated
   *Files On-Demand* placeholder. The pipeline triggers hydration and shows a
   **live download progress bar** (it polls the bytes actually on disk), because
   heavy files must finish downloading before they can be processed.
2. **Step 1 — epoching** (`actigraphy-epoching`, default settings): `.bin` →
   `<stem>_60s.csv` (60-second epochs, carrying `SVM_sum`) + metadata.
3. **Step 1 report** — the Actigraphy Sleep Report PDF.
4. **Compliance** — the *valid-day wear* rule on the 60-second `SVM_sum`
   epochs (see below). Produces a verdict + per-day table + JSON.
5. **Step 2 — sleep/circadian metrics** (`actigraphy-sleep-metrics`): IS, IV,
   M10/L5, periodograms, SRI, and a multi-page PDF.
6. **Excel** — a per-item workbook combining the compliance table and all Step 2
   metrics, plus a running participant-level summary.
7. **Replicate the raw `.bin`** into the output folder (optional).

### Data flow

```
staging/<CDxxx>/<Season>/Actigraph/<stem>.bin   (OneDrive placeholder)
        │  hydrate (progress)
        ▼
   Step 1  ──►  <stem>_60s.csv (+ .metadata.json)  ──►  Step 1 report PDF
        │                              │
        │                              ├──►  Compliance (valid-day wear)
        │                              └──►  Step 2 metrics  ──►  CSVs + PDF
        ▼
DASHBOARD TEST DATA/<CDxxx>/<Season>/Actigraph/   (all outputs + raw .bin replica)
```

---

## Install

```bash
python -m pip install -r requirements.txt   # orchestrator + dashboard deps
python setup_tools.py                        # clone the Step 1 & 2 tool repos from GitHub
#   python setup_tools.py --update           # …and git pull the latest next time
```

`setup_tools.py` clones the two standalone tool repos into `tools/` (paths and
GitHub URLs come from `config.yaml → tools`) and installs each tool's own
`numpy/pandas/scipy/matplotlib`. They are script repos, not pip packages, so they
are fetched this way rather than via `requirements.txt`. The interpreter named in
`tools.python_executable` must be able to run them.

> The **compliance** logic and its thresholds live in this project
> (`cdcompliance/compliance.py` + `config.yaml`), not in the tool repos — only
> Step 2's sleep metrics (incl. SRI) live in `actigraphy-sleep-metrics`.

## Configure

Copy the template and edit paths:

```bash
copy config.example.yaml config.yaml   # Windows
```

Key fields: `paths.source_root`, `paths.output_root`, `tools.epoching_repo`,
`tools.sleep_metrics_repo` (+ their `_url`s), `tools.python_executable`, and the
`compliance` thresholds. The **source / output roots and both tool-repo folders
are also editable at runtime** from the dashboard's top-right **⚙** panel, and
persist across sessions in `runtime_settings.json` — handy for different machines
/ users with different layouts.

## Run

```bash
# Preview (no download, no processing, no writes) — shows OneDrive state + sizes:
python run.py --dry-run

# Full run for the configured participant (CD011):
python run.py

# Scope it:
python run.py --seasons "Winter 2026,Spring 2026" --devices actigraph
python run.py --participant CD012 --no-copy-bin

# List devices:
python run.py --list-devices
```

Every run writes to `runs/<participant>_<timestamp>/`:
- `events.jsonl` — the full structured event stream (tailable by a UI).
- `run_summary.json` — per-item results and timings.
- `plan.json` — for `--dry-run`.

Exit code is non-zero if any item failed.

---

## Web dashboard

A two-tab dashboard (FastAPI + a static vanilla-JS page, WebSocket progress)
reuses the same core. Launch:

```bash
python -m pip install -r requirements.txt      # includes fastapi + uvicorn
python serve.py                                # or: uvicorn webapp.server:app
# open http://127.0.0.1:8000
```

**Tab 1 — Run Checks.** A grid of every `CD*` folder in the source staging tree,
each cell labelled with the suffix after `CD` (e.g. `011`):

- **grey** = not processed, **white** = fully processed, **grey + orange border**
  = partially processed (some seasons done, or new data appeared). States come
  from the *master output structure* (`manifest.py`).
- Click, or click-drag, to select cells; *Clear selection* resets. Selecting a
  single cell lists that participant's staging files ("WT OneDrive") with a ☁/●
  download indicator at the bottom.
- The centre **RUN CHECKS** button processes the selection (any participants,
  not just one), streaming live progress — OneDrive download %, per-step status,
  the tools' **verbose output line-by-line**, and verdicts — into the right panel
  and the MESSAGE/LOG. **Already-complete items are skipped** (selective
  processing) unless *force reprocess* is ticked. Finished cells turn white.
- Tick **dry run** to preview instead: it lists what would be processed and each
  input's OneDrive download state/size, with no download and no processing.
- The top-right **✕** button stops the server and frees the port.

**Tab 2 — Visualise.** A grid of the processed (output) folders. Select any set
and press **AGGREGATE** to see combined numbers — participant count, season
datasets (e.g. "3 summer, 2 winter"), and per-device passed/review/failed +
mean %compliance, broken down by season. Select a single folder, then pick a
`season · device` to see that recording's Step 2 measures and daily compliance.

The core stays UI-agnostic: the page only calls the JSON/WebSocket API in
[server.py](webapp/server.py); the pipeline itself is unchanged.

## Compliance rule — valid-day wear

Configured under `compliance:` in `config.yaml`.

| Concept | Definition |
|---|---|
| Active epoch | `SVM_sum >= activity_threshold_svm` |
| Valid day | active hours (`active_epochs / epochs_per_hour`) `>= min_valid_hours` |
| **PASS** | `valid_days >= min_valid_days` |
| **REVIEW** | `valid_days >= min_valid_days - review_margin_days` |
| **FAIL** | otherwise |

> **Calibrate `activity_threshold_svm`.** `SVM_sum` is the GENEActiv-style
> `sum(abs(vector_magnitude − 1))` per 60-s epoch; its scale depends on the
> device sample rate. The default (`5.0`) is a placeholder. Every compliance
> report includes each day's `SVM_sum` sum/mean/sd/median and the overall p90/p99
> so you can set the threshold against a night of known wear/non-wear.

---

## Output layout

```
DASHBOARD TEST DATA/
└── CD011/
    ├── CD011_compliance_summary.csv / .xlsx     (participant roll-up)
    └── <Season>/
        └── Actigraph/
            ├── <stem>.bin                        (raw replica, if copy_bin)
            ├── <stem>_60s.csv (+ .metadata.json) (Step 1 epochs)
            ├── <stem>_60s_sleep_report.pdf       (Step 1 report)
            ├── <stem>_60s_report.pdf             (Step 2 report)
            ├── <stem>_60s_{nonparametric,daily,periodogram,sri}.csv
            ├── <stem>_compliance.xlsx            (combined workbook)
            └── <stem>_compliance.json
```

---

## Architecture (built for a future web UI)

```
run.py                     thin CLI — arg parsing + event-sink wiring only
cdcompliance/
├── config.py              YAML config + RunSelection (a UI builds this)
├── events.py              EventBus + Console/JSONL sinks (a UI adds its own sink)
├── models.py              dependency-free dataclasses (JSON-serialisable)
├── onedrive.py            Files On-Demand hydration + download/copy progress
├── discovery.py           participant → season → device enumeration
├── tools.py               subprocess wrappers for Step 1 / Step 2
├── compliance.py          valid-day wear rule
├── excel.py               per-item workbook + participant roll-up
├── pipeline.py            run() / plan() orchestration
└── devices/
    ├── base.py            DeviceProcessor interface (+ NotImplemented stub)
    ├── actigraph.py       the implemented device
    └── registry.py        device key → processor (others are pending stubs)
```

**Adding a device later** = one new `devices/<name>.py` implementing
`DeviceProcessor` + one line in `registry.py`. Discovery, events, run summary,
and the CLI pick it up automatically.

**Web UI hook points:** build a `RunSelection`, register a custom event sink
(e.g. push each event to a websocket), and call `pipeline.run(...)` /
`pipeline.plan(...)`. No pipeline changes needed.

---

## Notes & assumptions

- Source `.bin` files and `raw/` data are treated as read-only; nothing is
  written back into the OneDrive source tree.
- Step 1/Step 2 are invoked as `python -m cli ...` with their repo as the
  working directory (they use relative imports).
- Step 2 writes into its own `outputs/` folder; the pipeline copies those files
  into the output tree.
- Season folder names are discovered, never hard-coded (they are inconsistently
  spaced in the source), and `--seasons` matching is whitespace/case-insensitive.
```
