# CD-Compliance-Checks

**Automatic wear-compliance checking for the Wellcome Trust CHiP-D study.**

It looks at a participant's device recordings in OneDrive, works out whether the
participant actually wore the device often enough, and writes the reports,
tables and spreadsheets into a results folder for you. There is a point-and-click
dashboard, so you never have to type commands once it is set up.

**Devices supported today:** Actigraph (GENEActiv `.bin` wrist recordings),
MiEYE (the M3 light logger) and Expiwell (experience-sampling surveys).
**Planned:** Saliva, Cognitron, Qualtrics.

---

## Contents

**Getting started (no programming knowledge needed)**
1. [What this actually does](#1-what-this-actually-does)
2. [What you need on your computer](#2-what-you-need-on-your-computer)
3. [Installing it, step by step](#3-installing-it-step-by-step)
4. [Telling it where your data lives](#4-telling-it-where-your-data-lives)
5. [Using the dashboard](#5-using-the-dashboard)
6. [Stopping it properly](#6-stopping-it-properly)
7. [When something goes wrong](#7-when-something-goes-wrong)
8. [Glossary](#8-glossary)

**Reference**
9. [What the checks actually measure](#9-what-the-checks-actually-measure)
10. [Where the results are saved](#10-where-the-results-are-saved)
11. [Command line usage](#11-command-line-usage)
12. [For developers](#12-for-developers)

> Architecture overview + system flowchart: [docs/pipeline.md](docs/pipeline.md).

---

## 1. What this actually does

Checking compliance by hand means opening each participant's folder, waiting for
a 1 GB file to download from OneDrive, running two separate analysis tools,
reading the numbers, and copying them into a spreadsheet — for every participant,
every season, every device.

This program does all of that in one click, and remembers what it has already
done so you never pay for the same work twice.

For one Actigraph recording it will:

1. **Download the file from OneDrive.** Big recordings are usually "online only"
   placeholders — the folder shows the file, but the data isn't on your PC yet.
   The dashboard shows a live download bar while it fetches it.
2. **Convert the raw recording into 60-second summaries** (this is the slow step).
3. **Produce the Step 1 sleep report PDF.**
4. **Decide whether the participant wore it enough** — PASS, REVIEW or FAIL,
   plus a day-by-day table.
5. **Calculate sleep and body-clock measures** (a second report PDF plus CSVs).
6. **Build an Excel workbook** for that recording, and add a row to a running
   summary for that participant.
7. **Copy the raw recording** into the results folder as an archive (optional).

For a **MiEYE** light recording it downloads the light CSV, runs the luminosity
tool (report PDF, per-day light measures, and a wear verdict based on whether the
day shows a real light/dark rhythm), and files the results the same way.

For an **Expiwell** folder it reads every experience-sampling survey export and
reports **what the participant actually answered** — item means, how each measure
moved across the study, and the sleep-diary metrics (time in bed, sleep onset
latency, wake after sleep onset, total sleep time, sleep efficiency) derived from
the Consensus Sleep Diary. Its report is a *measures* report: every page is
titled with the question it answers ("How was their mood?", "How well did they
sleep?"), not a wall of statistics.

### Devices covered

| Device | What it reads | What you get |
|---|---|---|
| **Actigraph** | GENEActiv `.bin` (~1 GB) | Wear compliance, sleep/body-clock metrics, 2 report PDFs |
| **MiEYE** | M3 light-logger CSV | Light exposure metrics + compliance, report PDF |
| **Expiwell** | 8 survey CSV exports | Item measures, sleep-diary metrics, question-per-page report |

Saliva, Cognitron and Qualtrics are recognised but not yet implemented.

---

## 2. What you need on your computer

### The essentials

| # | What | Why | How to check it's there |
|---|------|-----|-------------------------|
| 1 | **Windows 10/11 or macOS** | Runs on both; the live OneDrive download-% bar is most accurate on Windows | — |
| 2 | **Python 3.10 or newer** | This program is written in Python | Type `python --version` (macOS: `python3 --version`) |
| 3 | **Git** | Used once, to download the three analysis tools | Type `git --version` |
| 4 | **OneDrive**, signed in and syncing the CHiP-D repository | That's where the data comes from | The folder appears in File Explorer |
| 5 | **A web browser** (Edge, Chrome, Firefox) | The dashboard is a web page | Already installed |

### Things you do *not* need

People often expect a web dashboard to need extra web tooling. This one doesn't:

- **No Node.js, npm or JavaScript build step.** The dashboard is a plain web page
  that Python serves directly. There is nothing to compile.
- **No web server** (IIS, Apache, nginx). Python includes the server.
- **No database.** Everything lives in files and folders.
- **No internet connection at run time** — apart from OneDrive syncing your data.
  The one-off install does need internet.
- **No admin rights**, in most setups.

The dashboard runs only on *your* PC (at the address `127.0.0.1`, which means
"this computer"). Nobody else on the network can see it.

### Opening a terminal

Several steps below say "in a terminal". To open one:

**Windows**
- Press **Windows key**, type `powershell`, press **Enter**.
- Or: open the project folder in File Explorer, click the address bar, type
  `powershell` and press **Enter** — this opens a terminal already pointed at the
  right folder, which is what you want.

**macOS**
- Press **Cmd-Space**, type `Terminal`, press **Enter**.
- Then point it at the project folder: type `cd ` (with a space), drag the project
  folder onto the window, and press **Enter**.

A terminal is just a window where you type a command and press Enter. Text you
should type is shown in boxes like this:

```bash
python --version
```

### Installing Python (if `python --version` didn't work)

1. Go to <https://www.python.org/downloads/windows/> and download the latest
   **Windows installer (64-bit)**.
2. Run it. On the very first screen, **tick "Add python.exe to PATH"** at the
   bottom. This is the step people miss, and skipping it causes the
   `'python' is not recognized` error later.
3. Click *Install Now*, then close and reopen your terminal.
4. Check it worked: `python --version` should print something like `Python 3.13.1`.

> **Already have Anaconda?** That counts — you have Python. Use the *Anaconda
> Prompt* instead of PowerShell for all the commands below.

### Installing Git (if `git --version` didn't work)

Download it from <https://git-scm.com/download/win> and accept all the defaults.
Close and reopen your terminal afterwards.

---

## 3. Installing it, step by step

### The easy way — double-click

If the program is already installed, just double-click:

* **Windows** — `Start CD Dashboard.bat`
* **macOS** — `Start CD Dashboard.command`

> **If double-clicking says "permission denied" on macOS**, the file lost its
> executable flag (that flag does not survive a copy from Windows, a OneDrive
> sync, or a ZIP download). Start it once with either of these, and after that
> double-clicking works normally — the app repairs the flag itself on startup:
>
> ```bash
> bash "Start CD Dashboard.command"
> ```
> ```bash
> chmod +x "Start CD Dashboard.command"
> ```
>
> Two other macOS notes: the file must sit **inside the project folder** (next to
> `serve.py`) — it will not work from `Downloads`; and if macOS says the file is
> from an unidentified developer, right-click it → **Open** → **Open** once.

The first run sets everything up on its own — it builds a private Python
environment, installs the libraries, downloads the analysis tools, then starts the
dashboard and opens your browser. Later runs go straight to the dashboard.

If that works, skip to section 4. The manual steps below do the same thing.

### The manual way

Open a terminal **in the project folder** (the one containing `run.py`), then run
these two commands one at a time, waiting for each to finish.

> **macOS / Linux:** if `python` isn't found, use `python3` (and `pip3`)
> everywhere below — nothing else changes. The app works the same on Windows and
> Mac. (The live OneDrive *download %* bar is most accurate on Windows; on Mac the
> file is still fetched, just with a simpler progress display.)

**Step 1 — install the Python libraries this program uses:**

```bash
python -m pip install -r requirements.txt
```

This downloads pandas, numpy, openpyxl, FastAPI and uvicorn. It prints a lot of
text and takes a couple of minutes. Warnings in yellow are normal; only red
`ERROR` lines matter.

**Step 2 — download the analysis tools:**

```bash
python setup_tools.py
```

This uses Git to fetch the three analysis tools into **sibling folders next to
this project** — e.g. `actigraphy-epoching`, `actigraphy-sleep-metrics` and
`luminosity-metrics` sitting beside `CD-Compliance-Checks` — and installs what
*they* need (numpy, pandas, scipy, matplotlib). It takes a few minutes and ends
with `Tool setup complete.`

> You do **not** need to create a `config.yaml`. The app runs with sensible
> defaults; you point it at your data folders from the dashboard (next section).

Later on, to update those tools to their newest versions:

```bash
python setup_tools.py --update
```

That's the whole installation. You only do it once per computer.

---

## 4. Telling it where your data lives

The program needs to know two folders:

- **Source** — where the participant folders (`CD011`, `CD012`, …) sit in the
  synced OneDrive repository.
- **Output** — where you want the results written.

**The easy way:** start the dashboard (next section) and click the **⚙** button in
the top-right corner. Paste the two folder paths in and save. They are remembered
between sessions in `runtime_settings.json`.

> To copy a folder path in File Explorer: open the folder, click once on the
> address bar, and copy the highlighted text.

**The other way:** open `config.yaml` in Notepad and edit the top two lines:

```yaml
paths:
  source_root: 'N:\...\Welcome Trust - CHiP-D Data Repository\Tier 1\staging'
  output_root: 'N:\...\CHiP - D\DASHBOARD TEST DATA'
```

Keep the single quotes around the paths. The rest of that file is analysis
settings — see [section 9](#9-what-the-checks-actually-measure) before changing
any of them.

---

## 5. Using the dashboard

### The start page

The dashboard opens on a **start page** before the main screen. It lists this
program and each analysis tool, and checks GitHub for newer versions:

* **up to date** — nothing to do
* **N behind** — press **Update** to pull the newer version
* **not installed** — type the folder you want it in, then press **Install here**

Nothing starts on its own; press **Open dashboard** when you are ready. The tool
folders you choose are remembered between sessions.


Start it from a terminal in the project folder:

```bash
python serve.py
```

Leave that window open — it is the program itself; closing it stops everything.
Then open your browser at:

```
http://127.0.0.1:8000
```

### Compliance tab

The grid shows every participant folder found in the source location, labelled
with the digits after `CD` (so `011` is `CD011`). The colours mean:

| Colour | Meaning |
|--------|---------|
| **Grey** | Not processed yet |
| **White** | Fully processed |
| **Grey with an orange border** | Partly done — some seasons processed, or new data has appeared since |
| **Red ✕ badge** in the corner | An input file is present but **can't be processed** because of how it is named or placed |
| **Amber ! badge** in the corner | Processed, but the name doesn't reliably identify the file, or it was only found through a naming fallback |

To run checks:

1. **Click a cell** to select a participant. Click and drag to select several.
   *Clear selection* starts over. With exactly one selected, the panel at the
   bottom lists that participant's files with a ☁ (still in the cloud) or ●
   (downloaded) marker.
2. **Pick a run mode** (the buttons above the grid). The big button in the middle
   changes to match, and the text under it says exactly what will happen to the
   folders you've selected before you press anything:

   | Run mode | What it does | Use it when |
   |---|---|---|
   | **New & unfinished** *(default)* | Processes new samples and anything half-done; skips anything already complete | Normal use — including when a new season or file has been uploaded |
   | **Preview (dry run)** | Runs the naming check and lists what would be processed or skipped. Downloads and writes nothing | Before a real run, to check the plan |
   | **Force reprocess** | Redoes everything for the selected folders, including complete items. Asks for confirmation first, because Actigraph recordings are re-downloaded (~1 GB each) | After a threshold or tool change, when existing results must be regenerated |

3. **Press the button.** Progress appears live on the right, tagged with the
   mode (e.g. `RUN · new & unfinished`, `PREVIEW · dry run · done`). Cells turn
   white as they finish. If the selection contains files that can't be
   processed, a real run lists them and asks before starting.

**How a newly uploaded sample gets processed:** each folder is broken down into
season × device items, and an item counts as complete only when all its expected
results exist. A new season or file has no results yet, so **New & unfinished**
picks it up while skipping everything already done. An item that stopped part-way
resumes from its last finished step (for Actigraph, an existing 60-second file
means the 1 GB download and epoching are not repeated).

Other controls:

- **debug output** (beside the run mode) — print every line the analysis tools
  output in the log. It only changes what the log shows, not what the run does.
- **⚑ Flagged** (above the grid) — shows and selects only the folders with naming
  problems, with a count of how many there are.

### Batches and initials

Every run that writes data (**New & unfinished** or **Force reprocess**) is one
**batch** and needs your **initials** (2–4 letters, typed beside the run mode and
remembered for your account). Previews don't need initials and aren't recorded.

Batches are listed under **Batches · staging → master** below the log. The ✕ on
a batch **archives** it: it disappears from the list (tick *show archived* to
see it again) but its records are kept, with who archived it and when.

Each batch is saved in two parts (see `runs/README.md`):

- `runs/batches/` — a **public**, stripped record that is committed to the
  repository: initials, time, mode, counts, thresholds and tool versions. It
  never contains participant IDs, paths, filenames, serial numbers, dates of
  recordings or results; the code checks this before writing it.
- `runs/_private/` — the full detail (participants, per-item results, every
  event), which stays on your machine and is never committed.

The command line follows the same rule: `python run.py --participant CD012 --initials YK`.

### Naming check

Files are found by their folder and filename, so a file that drifts from the
naming convention can be silently skipped or processed under the wrong identity.
The **Naming check** box on the right changes with the selection:

- **One folder selected — expected vs current.** Every season and device folder
  is listed with its naming convention and an example (from the study's naming
  sheet), and under it each file the pipeline uses, shown by its current name
  with ✓ when it matches or the reason it doesn't.
- **Several folders selected — issues only.** A list of the problems across the
  selection, each with the file, what's wrong, and the expected pattern. Only files the pipeline actually processes are checked; the other
uploads (assessment logs, consent PDFs, screenshots) are ignored.

| Level | Meaning | Examples |
|---|---|---|
| **✕ skipped** | Data is present but won't be processed | a MiEYE file not named `-logged.csv`/`-download.csv`; a raw Actigraph export with no `.bin`; a second light file in the same season |
| **! warning** | Processed, but identity is unreliable or relied on a fallback | Expiwell export never renamed (`Affect.csv`); MiEYE still `-download.csv`; filename ID doesn't match its folder |
| **i info** | Processed fine; the name has drifted | missing season tag `S1`–`S4`; extra spaces; underscores instead of hyphens |

Where a file doesn't follow the convention but its folder still identifies it
unambiguously, it **is processed** and flagged so it can be renamed: un-renamed
Expiwell exports, MiEYE files still named `-download.csv`, and Expiwell names
missing `Data`. When several light files share a season, the one carrying the
participant's own ID is used — a stray test file can never be processed as that
participant.

The **Visualise** tab repeats this under *Combined compliance* as **Input
naming**, so a season missing from the numbers is explained there.

The patterns, messages and severities live in `cdcompliance/naming_rules.yaml`;
edit that file when a convention changes.

A full run on a fresh 1 GB recording takes a while — most of it is the OneDrive
download and the epoching step. You can press **STOP** at any point; it will
cancel the download or analysis in progress within a second or two and clean up
any half-written file.

### Visualise tab

A grid of the folders that have already been processed.

- Select any number of them and press **AGGREGATE** for combined numbers: how
  many participants, how many season datasets, and per-device passed / review /
  failed counts with mean % compliance, broken down by season.
- Select a single folder, then pick a `season · device` to see that recording's
  daily compliance and sleep measures.

### Sending a selection to T2

**SEND TO T2** (beside AGGREGATE) hands the selected folders to the T2 tab. It
writes **no data** — only a record of what was chosen.

1. Choose the **seasons**. These are seasons *of the year* — `Autumn 2025`,
   `Winter 2025/26` — worked out from the dates the recordings actually cover,
   not from the folder name. That matters: one person's folder may say
   `Winter 2025` and another's `Winter 2026` for the same January. The number on
   each chip is how many of the selected participants have data in that season.
2. Choose the **devices** and, if you want fewer, the **variables** (every
   variable in the data dictionary is listed with its unit and definition).
3. Press **Check availability**. This reads the processed outputs and reports:
   - each device's fully recorded days per participant-season, and the **common
     window** where all the chosen devices overlap (the days a cross-device
     comparison can actually use);
   - **missing items** — a device with no output, a missing 60 s file, an output
     file a chosen variable needs, or devices that never overlap. You can still
     continue: those days or devices are simply absent from the preview.
4. Tick the checks (the Actigraph 60 s outputs, and that you have reviewed the
   verdicts), enter your **initials**, and send.

The selection is recorded like a run: `T2-<yyyymmdd>-<hhmmss>-<initials>-s<seasons>`,
public stripped record plus private detail (see `runs/README.md`). Unlike
preprocess batches, T2 records **are** seasonal.

### T2 tab

T2 is a **preview**: it reads the processed outputs and writes nothing. Only
Export (a later phase) will produce a dataset.

- **Selections** (left) — everything sent from Visualise. ✕ archives one: it
  leaves the list but both records are kept, with who archived it and when.
  Archived selections can still be opened.
- **Days** — the range of days within each participant-season's shared window
  (day 1 = its first shared day), day by day, with shortcuts labelled by the days
  they cover (`days 1–7`, `days 8–15`). Beside it, **drop part-days** leaves out
  a first or last day that was only half recorded.
The panel has four views: **Coverage** (what data exists), **Agreement** (do two
measures in the same unit land on the same value), **Relationships** (does one
measure move with another) and **Compare** (one measure across groups, seasons
of the year, or people). Everything here already passed Visualise, so T2 does
not re-check compliance — every recorded day is included.

- **Availability** (Coverage) — the same coverage and flags as the send dialog, re-read
  from the outputs each time (so it reflects anything reprocessed since).
  Click a row to preview that participant-season.
- **Timelines** — the chosen variables on one shared date axis, with the common
  window shaded and the chosen weeks outlined. A filled dot is a valid day, a
  hollow dot an invalid one, and a faded dot a partly recorded day; hover for
  the date, value and window day. Up to 8 charts at a time — pick them with the
  **Charts** chips. Season-level values (IS, IV, SRI, response rates) are
  listed underneath.

- **Cross-device** — one variable against another, which is what a cross-device
  study is for. Pick x and y (from any device), then the **lag**:
  - *same day* — both on the same calendar date;
  - *that night* — y belongs to the night that starts on x's date;
  - *next day* — y is the following date (a sleep diary is filled in the
    morning after, so evening light usually pairs with the **next** day's diary).

  Two correlations are always shown, never one, because each participant
  contributes many days:
  - **Between-person** — each person's mean x against their mean y: *do people
    with more x tend to have more y?* n = participants.
  - **Within-person** — each person's days after subtracting their own mean:
    *on this person's higher-x days, is y higher too?* n = paired days, with an
    effective n that accounts for the person means.
  - **Median person r** — each person correlated separately, then the middle
    value; a sanity check that one person isn't driving the result.

  The scatter colours days by participant and marks each person's mean; the
  tick box switches to the person-mean-centred (within-person) view. 95 % CIs
  use the Fisher z transform with a normal approximation.

  The **correlation matrix** runs every pair of the chosen variables at that
  lag, starting with a spread across devices. Because many pairs are tested at
  once, read **q** (Benjamini–Hochberg FDR) rather than p; bold means q < 0.05.
  Click a cell to plot that pair. A cell shows "—" when there aren't enough
  paired days or a variable doesn't vary. At most 12 variables per matrix.

- **Seasons** — each variable per season number. Every cell is the mean of
  participant means (so someone with more days doesn't count more) ± the spread
  across participants, with how many participants contributed.

- **Compare** — one measure, split by your **groups**, by **season of the
  year**, by **participant**, or by visit number. Each bar is the average across
  people (not days, so nobody counts twice) with a 95 % interval, and the
  differences are listed underneath — worked out person by person when the same
  people appear on both sides. Groups are made here by clicking a person to move
  them between A, B, C and D; they are saved privately with the selection, and
  no demographics are involved.

- **Cut-offs** (beside the day range) — keep only days where a measure meets a
  rule, e.g. `act_wear_hours ≥ 22`. The slider is sized from the data, and the
  bar says how many days and people survive. This filters the numbers the tools
  already produced, so nothing is reprocessed; thresholds that are *baked into*
  those numbers (the light floor, the non-wear rule, the epoch length) are listed
  as needing a Compliance re-run instead.
- **Export…** — lists every file that would be written, with row and column
  counts, the destination and what is included — the data tables, a dictionary,
  a manifest, `report.pdf` with the charts, and a `reports/` folder holding the
  device report PDFs for the seasons being exported. Nothing is written until you
  enter the approver's initials and press Export; the folder then holds the data,
  a dictionary, a README and a manifest recording who approved it, the thresholds
  in force, each tool's version and a SHA-256 of every input file.

All of these honour the day slider and the filters, and they are exploratory:
they suggest what to look at, they don't test a pre-registered hypothesis.
A between-person correlation needs at least three people, and a difference needs
two on each side; otherwise a dash is shown with the reason.

Participant IDs shown here are the real ones until pseudonymous IDs are
approved and switched on; the pill by **Export** says which is in force.

---

## 6. Stopping it properly

Three ways, in order of preference:

1. **The ✕ button** in the dashboard's top-right corner. This cancels anything
   running, closes the program cleanly, and frees the port. Best option.
2. **Ctrl+C** in the black terminal window. The first press cancels any running
   check — stopping downloads, closing files and shutting down the analysis
   tools — then exits. It prints `Stopped.` when it's done. If something is truly
   wedged, **press Ctrl+C a second time** to force-quit immediately.
3. **Closing the terminal window** — works, but it's the blunt option: a running
   analysis tool can be left behind as an orphaned process.

> **Why this matters.** Earlier versions could hang on
> `Waiting for background tasks to complete` when you interrupted a run mid-way
> through a OneDrive download, and had to be killed by hand — the download had no
> way of hearing the stop request. That is fixed: stopping is now honoured
> immediately, anywhere in the process, and a part-copied file is deleted rather
> than left behind looking complete.

Nothing is ever lost by stopping. Finished recordings stay finished, and the next
run picks up where you left off.

---

## 7. When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `'python' is not recognized...` | Python isn't installed, or wasn't added to PATH | Reinstall Python and tick **"Add python.exe to PATH"** (section 2). Reopen the terminal |
| `'git' is not recognized...` | Git isn't installed | Install Git (section 2), reopen the terminal |
| `ModuleNotFoundError: No module named 'fastapi'` (or pandas, yaml, uvicorn…) | Step 1 of the install didn't finish | Re-run `python -m pip install -r requirements.txt` and read the output for red errors |
| `Config not found: config.yaml` | Step 2 of the install was skipped | Run `copy config.example.yaml config.yaml` |
| `[Errno 10048] address already in use` | The dashboard is already running in another window | Use that window, or start on another port: `python serve.py --port 8001` (then browse to `127.0.0.1:8001`) |
| The browser says the site can't be reached | The terminal window isn't running, or it stopped | Check the terminal for red error text; restart with `python serve.py` |
| The grid is empty | The source folder is wrong, or OneDrive hasn't synced | Check the path in the **⚙** panel — it reports whether each folder exists |
| A download bar sits at 0% for ages | OneDrive isn't fetching the file | Check the OneDrive icon in the system tray: signed in, not paused, not out of disk space. Try opening the file in File Explorer to force a download |
| `Timed out after 3600s downloading...` | The download took over an hour | Usually a OneDrive problem. Retry; if the connection is slow, raise `total_timeout_seconds` in `config.yaml` |
| `Step 1 finished but expected output not found` | The epoching tool failed | Tick **debug output** and re-run — the tool's own error will be in the log. Try `python setup_tools.py --update` |
| A run says `cancelled` | You pressed STOP or shut the server down | Nothing is broken. Re-run to continue where it left off |
| Everything is slow | Normal for a first run — the ~1 GB download and the epoching step dominate | Later runs skip finished work and are far quicker |

**Still stuck?** Every run writes a folder under `runs/` containing
`events.jsonl` (everything that happened) and `run_summary.json` (what each
recording produced). Send the newest one to whoever maintains this project.

---

## 8. Glossary

| Term | Plain English |
|---|---|
| **Terminal / PowerShell** | The black window where you type commands |
| **Python** | The programming language this is written in; it must be installed to run |
| **Git** | A tool for downloading code; used once during setup |
| **Files On-Demand / dehydrated / hydrating** | OneDrive shows files that aren't really on your PC yet. "Hydrating" is downloading one for real |
| **Epoching** | Squashing a raw recording (many readings per second) into one summary row per 60 seconds |
| **Epoch** | One of those 60-second summaries |
| **Valid day** | A day where the participant genuinely wore the device long enough to count |
| **PASS / REVIEW / FAIL** | Enough valid days / just short (worth a human look) / not enough |
| **SVM** | A single number for how much movement happened in an epoch |
| **Melanopic lux** | Light measured the way the body clock responds to it, rather than how bright it looks |
| **Preview (dry run)** | A run mode that changes nothing: naming check plus what a run would do |
| **Port 8000** | The "channel number" the dashboard uses on your own PC |

---

## 9. What the checks actually measure

### Actigraph — the valid-day wear rule

Set under `compliance:` in `config.yaml`.

| Concept | Definition |
|---|---|
| Non-wear epoch | At least 2 of 3 axis SDs below `nonwear_axis_sd_g` (device motionless), sustained for `nonwear_min_minutes` |
| Valid day | At least `min_valid_hours` of wear **and** an hourly-mean SVM range of at least `min_diurnal_range_svm` (rejects flat, implausible days) |
| **PASS** | `valid_days >= min_valid_days` |
| **REVIEW** | `valid_days >= min_valid_days - review_margin_days` |
| **FAIL** | otherwise |

> **⚠ `activity_threshold_svm` still needs calibrating.** `SVM_sum` is the
> GENEActiv-style `sum(abs(vector_magnitude − 1))` per 60-second epoch, and its
> scale depends on the device's sample rate. The default (`5.0`) is a
> placeholder used only as a fallback for inputs without axis SDs. Every
> compliance report includes each day's `SVM_sum` sum/mean/sd/median plus the
> overall p90/p99, so you can set the threshold against a night of known
> wear/non-wear before treating verdicts as authoritative.

### MiEYE — the light-based valid-day rule

Set under `luminosity:` in `config.yaml`. The device is charged regularly, so the
charger flag is **not** a wear signal. Instead a valid day needs:

- **Diurnal variation** — daytime melanopic light at least `min_day_night_ratio`×
  the night level (a real day/night rhythm, so a device left in a drawer fails).
- **Light activity** — at least `min_light_hours` (≈14 h expected) of epochs above
  `light_floor_lx`.

Daytime/night melanopic EDI, TAT250 and TBT10 are reported as secondary
light-adequacy statistics (Brown et al. 2022). The report PDF includes channel
heatmaps, distribution histograms, daily light dose over the study, per-day
time-in-zones, per-day cumulative exposure and per-day distributions — each page
captioned with the question it answers.

> Actigraph compliance logic lives in this project (`cdcompliance/compliance.py`
> + `config.yaml`). Sleep metrics (including SRI) live in
> `actigraphy-sleep-metrics`. MiEYE compliance lives in `luminosity-metrics` so
> that tool works standalone, with its thresholds configurable here.

### Data flow (Actigraph)

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

### Expiwell — reported measures, not a wear rule

Expiwell is an experience-sampling app, so there is no device to wear. The tool
reports **what the participant answered**:

| Measure | How it is obtained |
|---|---|
| Item measures | Every question with a numeric answer: n, mean, SD, median, range. Answers export as text labels ("Very little"), so they are decoded to numbers using each question's own scale legend. |
| Positive / negative affect | The 1–7 mood grid split into pleasant items (satisfied, relaxed, cheerful, energetic, enthusiastic, calm) and unpleasant items (upset, irritated, listless, down, nervous, bored, anxious), then averaged separately — mixing them would cancel out. |
| Sleepiness | Karolinska Sleepiness Scale, 1 (extremely alert) – 9 (very sleepy). |
| Sleep metrics | Derived from the Consensus Sleep Diary: time in bed, sleep onset latency, wake after sleep onset, number of awakenings, total sleep time and sleep efficiency. |

A response rate is still calculated (so the dashboard can colour the grid), but
the report itself is about the measures.

> **Note.** An Expiwell export contains only *completed* responses — a missed
> prompt leaves no row at all. The number of prompts expected therefore comes
> from the configured schedule, not from the file.

---

## 10. Where the results are saved

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
        └── MiEYE/
            ├── <stem>_luminosity_report.pdf      (heatmaps, histograms, per-day pages)
            ├── <stem>_luminosity_metrics.csv     (per-day light-adequacy metrics)
            ├── <stem>_daily_compliance.csv       (per-day wear / %compliance)
            └── <stem>_compliance.json            (verdict + summary)
```

Start with the `_compliance.xlsx` workbook for one recording, or the participant
`_compliance_summary.xlsx` for the overall picture.

---

## 11. Command line usage

The dashboard is the recommended interface. The CLI does the same work without a
browser — useful for scripting or scheduled runs.

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

Exit codes: `0` success, `1` something failed, `130` interrupted with Ctrl+C
(which also cancels any download in progress and shuts down the analysis tools).

Dashboard options:

```bash
python serve.py --port 8001      # if 8000 is taken
python serve.py --host 0.0.0.0   # expose on the network (not recommended)
python serve.py --reload         # developer auto-restart on code changes
```

---

## 12. For developers

The core is UI-agnostic: the CLI and the web UI drive the same engine and consume
the same structured progress event stream.

```
run.py                     thin CLI — arg parsing + event-sink wiring only
serve.py                   dashboard launcher (uvicorn + two-stage Ctrl+C)
cdcompliance/
├── config.py              YAML config + RunSelection (a UI builds this)
├── events.py              EventBus + Console/JSONL sinks (a UI adds its own sink)
├── models.py              dependency-free dataclasses (JSON-serialisable)
├── onedrive.py            Files On-Demand hydration + download/copy progress
├── discovery.py           participant → season → device enumeration
├── tools.py               subprocess wrappers for Step 1 / Step 2 / luminosity
├── compliance.py          valid-day wear rule
├── excel.py               per-item workbook + participant roll-up
├── pipeline.py            run() / plan() orchestration
└── devices/
    ├── base.py            DeviceProcessor interface (+ NotImplemented stub)
    ├── actigraph.py       Actigraph (GENEActiv .bin)
    ├── mieye.py           MiEYE (M3 light logger → luminosity-metrics tool)
    └── registry.py        device key → processor (others are pending stubs)
webapp/
├── server.py              FastAPI JSON + WebSocket API
├── jobs.py                job queue, worker thread, event fan-out
└── static/                the dashboard page (vanilla JS, no build step)
```

**Adding a device** = one new `devices/<name>.py` implementing `DeviceProcessor`
plus one line in `registry.py`. Discovery, events, run summary and the CLI pick
it up automatically.

**Web UI hook points:** build a `RunSelection`, register a custom event sink
(e.g. push each event to a websocket), and call `pipeline.run(...)` /
`pipeline.plan(...)`. No pipeline changes needed.

### Cancellation model

Stopping must work *during* a long stage, not only between recordings — a single
hydration can run for many minutes.

- A job owns a `threading.Event`. STOP, `/api/shutdown` and server shutdown all
  set it and call `tools.terminate_all()` to kill live tool subprocesses.
- `pipeline.run()` puts that event on the `ResolvedSelection`, so device
  processors pass it to `onedrive.ensure_local()` and
  `onedrive.copy_with_progress()`. Both check it every poll/chunk and raise
  `onedrive.DownloadCancelled`; a cancelled copy deletes its partial destination.
- A subprocess we killed raises `tools.ToolCancelled`, so it is reported as
  cancelled rather than as a tool failure.
- Processors catch both and return `status="cancelled"` with an `item_cancelled`
  event; the run emits `run_cancelled`.
- The WebSocket handler races `queue.get()` against the socket closing, because
  uvicorn closes connections *before* running the lifespan shutdown hook — the
  handler must not be what graceful shutdown is waiting on.

### Notes & assumptions

- Source `.bin` files and `raw/` data are read-only; nothing is written back into
  the OneDrive source tree.
- Files are hydrated by *reading* them, never by pinning (`attrib +P`) — pinning
  would leave OneDrive keeping every ~1 GB `.bin` on disk forever, even after
  this program exits.
- Step 1/Step 2 are invoked as `python -m cli ...` with their repo as the working
  directory (they use relative imports).
- Step 2 writes into its own `outputs/` folder; the pipeline copies those files
  into the output tree.
- Season folder names are discovered, never hard-coded (they are inconsistently
  spaced in the source), and `--seasons` matching is whitespace/case-insensitive.
