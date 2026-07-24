# CHiP-D Compliance — System Architecture

> **The flowchart lives in [`cd-compliance-pipeline.canvas`](cd-compliance-pipeline.canvas)**
> (open in Obsidian). It shows all three pipes side by side: **Pipe 1** Actigraph
> Run-Checks, **Pipe 2** Visualise, and **Pipe 3** MiEYE luminosity. This page is
> the text companion to that canvas.

How the whole pipeline fits together: from the OneDrive staging tree, through the
CD-Compliance-Checks orchestrator and its external device tools, into the
dashboard output tree and the two-tab web UI.

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
  `actigraphy-sleep-metrics` (Step 2), `luminosity-metrics` (MiEYE).
- **Output tree** `DASHBOARD TEST DATA/CDxxx/Season/<Device>/` → read back by the
  device-agnostic **Visualise** panel.

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
