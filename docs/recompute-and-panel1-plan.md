# Plan · Recompute variants, Panel 1 status, pseudonymous export

Revised 2026-10-08 after narrowing the scope of Recompute. Supersedes sections
B and D of `item-level-and-recompute-plan.md`; sections A, C and E of that
document still stand.

Scope, as agreed:

- Recompute applies to a participant **already selected into T2**.
- It re-runs **step 1 only**, with a different setting (e.g. a 30 s epoch).
- **Step 2 is left completely intact.**
- The result is an **extra** data point in the master folder, never a
  replacement.
- It is reported in T2 so it can be investigated before export.
- A variant that turns out not to be needed must be **removable**, along with
  its export and its tagged file in master.
- **A recompute does not change compliance.** The 60 s verdict stands.
- The calibration wrapper is out of scope and is not being wired in.

---

## A. Does a 30 s epoch break the algorithms?

This was the real risk, and the answer is better than expected: **step 2 needs
no changes at all.** It is already epoch-aware. But the orchestrator's own
compliance code is not — and that matters less than it looks.

### Safe: IS, IV, M10, L5, RA, periodograms

Every one of these is computed from a **day × hour matrix**, not from the epoch
series. `preprocess.aggregateHours` floors each epoch to its calendar hour and
takes the mean, so at 30 s it averages 120 epochs per hour instead of 60 and
hands back a matrix of exactly the same shape (D × 24). `cli.py` then feeds
that matrix to `interdailyStability`, `intradailyVariability`, `compute_M10`,
`compute_L5`, `compute_daily_metrics` and both periodograms.

So these measures are **structurally invariant** to the epoch length. The
numbers will move slightly, because an hourly mean of 120 finer epochs carries
less within-hour noise than a mean of 60 — that is a real property of the
finer sampling, not an artefact. Nothing is misaligned and nothing is
mis-scaled.

The only hard limit is `delta > 60` minutes, which `cli.py:125` exits on. 30 s
passes comfortably. Going *coarser* than 60 minutes is the direction that
breaks, and nobody is asking for that.

### Partly safe: SRI

Step 2 already does the right thing structurally. `cli.py:200` infers the epoch
length from the data itself:

```python
epoch_sec = int(round(delta * 60))
sri_results = compute_SRI(d, epoch_sec=epoch_sec, verbose=v)
```

so `_dayByEpochMatrix` builds `M = 86400 // epoch_sec` columns — 2880 at 30 s
instead of 1440 — and the clock-time alignment stays correct. There is no
silent grid collision.

It also normalises the amplitude. `sri.py:193` divides the epoch value by
`epoch_sec` before scoring:

```python
activity = s.values / float(epoch_sec)
```

This matters more than it looks. `SVM_sum` is a **sum** over samples
(`actigraphy-epoching/preprocess/epoch.py:48`), so a 30 s epoch carries about
half the value of a 60 s one. Dividing by `epoch_sec` turns both into a
per-second rate, so the halving cancels exactly and Cole–Kripke's absolute
`D < 1` threshold still sees a comparable quantity.

So two of the three things one might worry about are already handled: the
**grid** is correct and the **amplitude** is correct. What remains is the
**time span of the kernel**. Cole–Kripke's seven weights
`[106, 54, 58, 76, 230, 74, 67]` are defined for 1-minute epochs, so at 60 s the
window covers 7 minutes of real time and at 30 s it covers 3.5 minutes. The
scoring therefore smooths over half as much real time, flips state more
readily, and biases SRI **downward**. `sri.py:173` warns whenever
`epoch_sec != 60`.

**Consequence:** a 30 s SRI is not wildly wrong, it is *differently smoothed*.
The gap between it and the 60 s value is the kernel, not the participant — so
it is interpretable for development, and must not leave a variant without that
warning attached.

### Would break, but will never run: compliance

`cdcompliance/compliance.py` assumes one epoch is one minute in two places:

- `compliance.py:97` — `_sustained(candidate, cfg.nonwear_min_minutes)` passes
  a count of **minutes** where the function wants a count of **epochs**. At
  30 s a 30-minute non-wear rule silently becomes a 15-minute rule.
- `cfg.epochs_per_hour` (fixed at 60) converts epochs to hours. At 30 s every
  wear-hour figure would be doubled, so a 5-hour day would read as 10 and pass
  the valid-day test.

Both would produce a plausible-looking but wrong verdict, which is the worst
kind of bug.

**This resolves itself.** Because a recompute does not change compliance, a
variant should **not run compliance at all** — no `evaluate()`, no
`_compliance.json`, no `_daily_compliance.csv`, no `_append_summary` row, no
grid colour. The 60 s baseline remains the single source of every verdict. The
two epoch-dependent bugs then never execute.

That is a design constraint worth writing down rather than leaving implicit:

> **A variant produces measures, never verdicts.**

If anyone later decides variants *should* carry verdicts, those two lines must
be fixed first. I would add a guard — `evaluate()` raises if
`epochs_per_hour` disagrees with the epoch interval it reads from the CSV — so
the bug cannot be reintroduced by accident.

### Linear vs parallel, which was the underlying worry

The device lanes are parallel and independent: a variant on Actigraph cannot
affect MiEYE or ExpiWell, because they share no file and no code path. Within
the Actigraph lane the chain is linear — step 1 → step 2 → compliance — and a
variant forks it at step 1 and **stops before compliance**. T2 then reads
whichever branch it is pointed at. Nothing downstream is shared mutable state,
so the fork is safe.

The one genuinely shared thing is `<participant>_compliance_summary.csv`, the
roll-up `_append_summary` appends to. A variant must not touch it. That is one
`if` statement.

---

## B. Building Recompute

### B1 · Thread the epoch through step 1

`tools.run_step1_process` gains an `epoch: str = "60s"` argument, passes
`--epoch`, and expects `<stem>_<epoch_label>.csv` rather than a hard-coded
`_60s`. Step 1 already names its output that way (`preprocess/io.py:39`), so
nothing on that side changes.

The existing hard-coded `_60s` sites stay as they are, because they all
describe **the baseline**, which is still 60 s:

- `devices/actigraph.py:96,139` — completeness and the resume anchor
- `results.py:238` — the Panel 2 drill-down
- `t2_data.py:171,397` — the globs T2 reads

Keeping them untouched is what guarantees a variant can never change a grid
colour or a verdict.

### Decisions taken (2026-10-08)

| Question | Decision |
|---|---|
| What a recompute creates | **Epoch CSV only.** Step 2 is a second, explicit action from T2. |
| Where variants live | **`_variants/` inside the master device folder**, beside the baseline. |
| How it is surfaced | **Visible and enabled, tagged `experimental`** in the modal, in `variant.json` and in the batch record. |
| Missing raw `.bin` | **Refuse**, naming the recording that needs a force reprocess. Recompute never touches OneDrive. |
| Compliance on a variant | **Never run.** The 60 s verdict is the only verdict. |
| Cohort lineage | A recompute makes a **derived cohort**: a **subset** of its parent, never larger, and linked to it. Visualise always makes **fresh** cohorts. |
| Pseudonymisation | **Stays off.** Add a settings location editor first (see E). |

### B2 · Where a variant lives

```
<output>/<CDxxx>/<Season>/Actigraph/
    <stem>_60s.csv                      baseline — untouched
    <stem>_compliance.json              baseline verdict — untouched
    _variants/
        epoch-30s-20261008-143000-YK/
            variant.json                 always
            <stem>_30s.csv               always - this is the whole default output
            step2/                       ONLY if someone later asks for it
                <stem>_30s_nonparametric.csv
                <stem>_30s_daily.csv
                <stem>_30s_periodogram.csv
                <stem>_30s_sri.csv
                <stem>_30s_report.pdf
```

**A recompute writes two files.** `variant.json` and the epoch CSV, and
nothing else. Step 2 is a separate action, offered from the variant's row in
T2 (*run step 2 on this variant*), and it writes into `step2/` only because
somebody pressed it. Step 2 on an existing epoch CSV is seconds of compute,
so there is no reason to do it speculatively, and it keeps the promise that
nothing appears in master that nobody asked for.

A `_variants/` subfolder rather than loose files, for three reasons: nothing
that globs the device folder picks a variant up by accident; the whole thing is
one directory to delete; and the folder name itself is the tag
(`epoch-30s-<when>-<initials>`).

`variant.json` records what makes it reproducible and reversible:

```json
{
  "variant_id": "epoch-30s-20261008-143000-YK",
  "batch_id":   "RECOMP-20261008-143000-YK",
  "participant": "CD233", "season": "Summer 2026", "device": "actigraph",
  "input_stem": "<stem>",
  "changed": {"epoch": {"baseline": "60s", "variant": "30s"}},
  "step1_args": ["--epoch", "30s"],
  "step2": "unchanged",
  "compliance": "not evaluated - baseline verdict applies",
  "warnings": ["SRI: Cole-Kripke weights are calibrated for 60 s epochs"],
  "by": "YK", "at": "2026-10-08T14:30:00",
  "tool_versions": {"step1": "<sha>", "step2": "<sha>"}
}
```

### B2b · Cohort lineage — fresh vs derived

A recompute does not only produce files. It produces a **derived cohort**, and
cohorts now come in two kinds:

| Kind | Where it comes from | Parent |
|---|---|---|
| **Fresh** | Visualise → **SEND TO T2**. A new selection chosen from the grid. | none — it is a root |
| **Derived** | **Recompute** on a cohort that already exists in T2. | the cohort it was recomputed from |

**The invariant: a derived cohort is a subset of its parent — never larger.**
You can recompute all of a cohort, or any part of it, but you can never pick up
a participant-season that was not in the parent. If you want more, that is a
*fresh* cohort from Visualise, not a recompute.

Enforced in `t2_selections.send()`:

```python
if sel.parent_batch_id:
    parent = read_selection(config, sel.parent_batch_id)
    extra = set(sel.keys()) - set(parent.keys())        # (participant, season, device)
    if extra:
        raise SelectionError(
            "a recompute cohort cannot be larger than the one it came from; "
            "these are not in the parent: " + ", ".join(sorted(extra)))
```

`T2Selection` gains `parent_batch_id: str | None` and `derived_from: str | None`
(the `RECOMP` batch that produced it). Both go in the **private** record;
the public record carries only `"derived": true` and the parent's **stage and
date**, never its id, since a batch id is not identifying but the pairing adds
nothing public.

**Why the linkage matters.** Without it, a derived cohort looks like any other
selection in the T2 list, and six weeks later nobody can tell that
`T2-20261103-YK-s1` is the 30 s re-read of `T2-20260918-001143-YK-s1`. With it,
the T2 panel can show the list as a tree — fresh cohorts as roots, derived ones
nested beneath with the parameter that produced them:

```
T2-20260918-001143-YK-s1          fresh · 12 participants
  └─ T2-20261103-143000-YK-s1     derived · epoch 30 s · 4 participants
```

and `discard` (§ C) can walk it: discarding a parent must refuse while a
derived cohort still points at it, exactly as it already refuses while an
export points at a variant.

### B3 · A new batch stage

`batches.STAGES` gains `"RECOMP": "master -> variant (recompute)"`. The public
record carries the stage, the parameter changed with its two values, the
counts, the thresholds and the tool commits — no IDs, no paths, no filenames,
so `assert_public_safe` passes unchanged. The private record carries the
participant-seasons and the variant ids.

This gives the public history a defensible line: *on 8 Oct, YK made 30 s epoch
variants for 4 participant-seasons using step 1 at commit abc1234* — with no
participant identifiable.

### B4 · The T2 side

- T2's recording list shows the baseline plus any variants beneath it,
  flagged `variant · 30 s epoch`, with the warning from `variant.json` shown
  next to any measure it applies to.
- **Recompute…** becomes enabled. It opens a modal scoped to the current
  selection: pick participant-seasons, pick the parameter, read what will
  re-run and roughly how long, enter initials, go. It submits a `RECOMP` job
  through the existing `JobManager`, so progress, STOP and the log all work as
  they already do.
- Variants are **opt-in per participant-season** at export time. The default
  is the baseline. `manifest.json` records, per participant-season, which
  branch fed it — `"variant": null` for the baseline.

### B5 · What Recompute cannot vary

Step 2 exposes only `--measures`, so nothing inside it can be changed from
here, which is exactly the agreed scope. What *can* be varied is step 1:
`--epoch`, `--filter` / `--low` / `--high`, `--summary-mode`. I would ship
`--epoch` first and leave the filter options behind the same modal for later,
since a bandpass change affects SVM itself and raises the comparability
question far more sharply than an epoch change does.

---

## C. Removing a variant

There is **no delete path anywhere in the codebase today** — not for outputs,
not for exports. `batches.archive_batch` moves a record to `_archive/`, and
`t2_export` only ever creates. So this is genuinely new, and it is the one
place in this plan that destroys data, so it should be the most conservative.

Proposal — `cdcompliance/variants.py`:

```
discard(config, variant_id, initials, *, also_exports=False)
```

1. Refuse unless the variant exists and its `variant.json` parses.
2. Find every export whose `manifest.json` references the variant.
   - With `also_exports=False` (default) and any export found: **refuse**, and
     list them. A variant that fed a delivered dataset cannot quietly vanish.
   - With `also_exports=True`: move each export folder to
     `<t2_root>/_discarded/<export-id>/`.
3. Move the variant folder to
   `<output>/.../Actigraph/_variants/_discarded/<variant-id>/`.
4. Append a `discarded` block to `variant.json`: who, when, why, and which
   exports went with it.
5. Write a `RECOMP` batch record with `status: "discarded"`, so the public
   history shows the variant was made **and** withdrawn.

**Move, not delete.** A study audit trail should be able to answer "what
happened to that 30 s run?" with something other than silence. A separate,
explicitly-confirmed *purge* of `_discarded/` can empty it later; that is the
only operation that truly unlinks, and it should ask for the variant id typed
out in full.

Baseline files are never touched by any of this. `discard` refuses to operate
on anything outside a `_variants/` folder.

---

## D. Panel 1 right side · a status panel

Today the right column is Progress (live, empty until a run), Naming check, and
the WT OneDrive file tree. Nothing there says what has and has not been
processed; that lives only in the left-hand grid colours, at participant
granularity.

Nothing is removed. The column is restacked, and one box is added.

```
┌─ right column ─────────────────────────────┐
│ Processing status            ← NEW         │
│   device buttons:                          │
│     [Actigraph] [ExpiWell] [MiEYE]         │
│     [Cognitron] [Qualtrics] [Saliva]  grey │
│   per season, for the selected participant:│
│     S1 ✓ done   S2 ◐ partial               │
│     S3 ○ not processed   S4 — no data      │
│                                            │
│ Progress              (unchanged)          │
│ Naming check          (unchanged)          │
├────────────────────────────────────────────┤
│ ▸ OneDrive · current processing   ← collapsed, bottom │
└────────────────────────────────────────────┘
```

**Device buttons.** `actigraph`, `expiwell`, `mieye` live;
`cognitron`, `qualtrics`, `saliva` greyed out and non-interactive, labelled
*not wired in yet*. These come straight from the registry — `implemented_devices()`
versus `all_devices()` — so a device becomes live the moment its processor is
registered, with no UI change. Clicking a live device filters the season rows
below to that device.

**Season rows.** One row per season folder for the selected participant, with a
state per device: `done` / `partial` / `not processed` / `no data`, taken from
the `/api/participant/{pid}/items` endpoint in section B of the earlier plan.
This is where the per-item **Process** and **Reprocess** buttons from that plan
belong, so the two pieces of work land in the same box rather than competing
for the same space.

**OneDrive, collapsed at the bottom.** The existing file tree becomes a
collapsible strip that shows, when open, the hydration state of the files for
the current selection and, during a run, the file currently downloading with
its percentage. That is information `onedrive.ensure_local` already emits on
the bus; it is simply not surfaced anywhere persistent.

**Why this complements rather than duplicates the left panel.** The grid
answers *which participants need attention* across the whole cohort — one
colour per person. This box answers *what exactly is outstanding for the one
person I have selected*, broken down by device and season, with the buttons to
act on it. The left panel is the sweep; the right panel is the detail. Today
the detail only exists mid-run and then disappears.

No existing element is moved out of the panel or removed.

---

## E. Pseudonymous export and the dictionary

### What is already built

`pseudo_id.py` is complete and tested-by-construction: a keyed
**HMAC-SHA256(key, normalised ID)**, encoded to 10 characters from a 30-symbol
alphabet that deliberately omits `c`, `d`, `i`, `l`, `o`, `u` — so a PID can
never look like `CD` + digits, and therefore can never trip the public-record
leak guard. Collisions are checked. The key is 32 random bytes in a file
outside the repo; `key_fingerprint()` identifies which key was used without
revealing it.

`t2_export` already calls `pseudo_id.mapping` and maps the participant column
(`t2_export.py:82`), and already refuses to copy the device report PDFs when
pseudonymisation is on, because their filenames carry the real ID
(`t2_export.py:154`).

So the export path is wired. Three things are missing.

### What is missing

**1 · The switch is off.** `privacy.pseudonymise: false`. Turning it on is one
line, but it should not be flipped until the key exists and its custody is
agreed, because every PID is a function of the key — rotate the key and every
previously exported dataset becomes unlinkable to every new one.

**2 · There is no dictionary.** Nothing writes an ID → PID table. This is the
piece being asked for, and the important decision is **where it lives**, since
a dictionary inside the export would defeat the whole exercise. Recommendation:

- the key file and the dictionary live together on the **restricted study
  drive**, never in either git repository and never under `t2_root`;
- the dictionary is **derived, not stored as truth** — `pseudo_id.mapping` can
  regenerate it from the key at any time, so what gets written is a
  convenience file, and losing it costs nothing while losing the key costs
  everything;
- each export's `manifest.json` records only the **key fingerprint**, so a
  dataset can be proved to belong to a given key era without exposing it;
- a `cdcompliance dictionary --out <path>` command writes it on demand, with a
  header stating it is identifying data and must not be shared with the T2
  dataset.

**3 · The report PDFs are dropped.** With pseudonymisation on, an export
currently loses them entirely, which is a real loss of reviewable evidence. Two
options, in question 3 below.

### Decided, and what it means for the build

**Pseudonymisation stays OFF.** Key custody needs discussing with the study
team first, and the location will be chosen then. So the build order is:

1. **Add a location editor to Settings**, exactly like the existing Source /
   Output / T2 root fields. Two new rows — *Pseudonymisation key file* and
   *ID dictionary folder* — written to the same per-user profile as the other
   paths, validated (folder exists, is writable, is **not** inside either git
   repo and **not** inside `t2_root`), and shown with a clear "identifying
   data — restricted location" warning. `privacy.key_file` already exists in
   the config dataclass, so this is a UI and validation change, not a schema
   one.
2. **Leave `privacy.pseudonymise: false`.** Every screen, record and export
   keeps the real ID, exactly as today.
3. **Flip it only once** the location is set, the key is generated on that
   location, and the team has agreed who holds it.

**Where IDs are real and where they are not.** Confirmed intent:

| Stage | ID shown |
|---|---|
| Staging, master / output, Panel 1, Visualise, T2 preview | **real `CDxxx`** — unchanged |
| T2 **export** only | **pseudonymised** |

So pseudonymisation is a property of the export boundary alone. Nothing
upstream changes, which is also what keeps the pre-processing folders
navigable by the people running them.

**The report PDFs — flagged, not built.** With pseudonymisation on, the
filenames carry the real ID, and the ID may also be printed *inside* the PDF
by step 1 and step 2. Renaming the file does not fix the content. Doing this
properly means those two tool repos would have to accept a display name for
their report headers — and that is out of scope for now. **Flagged for a
decision before anything is changed in the wired-in repositories.** Until
then an export with pseudonymisation on continues to omit the PDFs, which is
the safe failure.

The remaining `pseudo_id.py` docstring questions: prefix `P` (the current
default) is fine, and 10 characters is ample for ~11,000 possible IDs with
collision room to spare — I would not shorten it. Neither needs a decision
before the location editor is built.

---

## F. The tabulated measures document

Asked for: an expansion of the flowchart as a table, giving for each measure
the mathematical transformation and the levels used to decide compliance.

Proposed shape — one row per measure, grouped by device, with columns:

| Measure | What it means | Formula | Inputs | Units / range | Threshold | Where decided |
|---|---|---|---|---|---|---|

Covering, from the code rather than from the papers:

- **Step 1** — SVM per epoch; the epoch aggregation itself.
- **Compliance (actigraph)** — the non-wear rule, the valid-day rule, the
  diurnal-range rule, and the PASS / REVIEW / FAIL ladder, each with the
  config key that sets it and its current value.
- **Step 2** — IS, IV, M10, L5, RA, L5:M10, the two periodograms, SRI with its
  Cole–Kripke stage written out, and the per-day noon-to-noon window.
- **MiEYE** — MEDI day and night, TAT 250, TBT 10, M5 / M7 / L2, and the light
  adequacy rule.
- **ExpiWell** — response rate with its configured denominator, and the survey
  types.
- **T2** — the circular clock-hour difference, the common window, within- vs
  between-person correlation, FDR, and the cut-offs that filter without
  recomputing.

Each row will also carry a **sensitivity note** saying whether the measure is
invariant to the epoch length — which, after section A, is now known for every
actigraph measure and is exactly what makes a 30 s variant interpretable.

Format question in question 4.

---

## Suggested order

1. **A's guard** — make `evaluate()` refuse an epoch interval that disagrees
   with `epochs_per_hour`. Tiny, and it closes the only genuine correctness
   hazard in this whole plan before any of the rest is built.
2. **D + item-level runs** — the Panel 1 status box, which is also where the
   per-item buttons live.
3. **B1–B3** — epoch threading, the variant folder, the `RECOMP` stage. CLI
   only, no UI.
4. **C** — discard, before any variant exists in anger.
5. **B4** — the Recompute modal and the T2 wiring.
6. **E1** — the Settings location editor for the key and dictionary. Can be
   built now; it does not turn anything on.
7. **E2** — flip pseudonymisation on, once key custody is settled, and decide
   the report-PDF question before touching the tool repos.

**The tabulated document (F) is done** — `docs/measures.md`, with
`docs/measures-print.html` generated from it for A4 landscape. Its
epoch-length summary is the reference for interpreting any variant.
