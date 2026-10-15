# Plan · item-level processing, staleness, and Recompute

A status review of three things the dashboard is asked to do, what the code
actually does today, and what it would take. Written 2026-10-07 against
`92e1018` (v0.0.6).

---

## A. Where the pipeline stands today

| Claim | Verdict | Evidence |
|---|---|---|
| staging → master/output, then pick what goes to T2 | **correct** | `pipeline.run` writes under `paths.output_root`; `t2_selections` records a selection, `t2_data` reads `output_root` |
| T2 is preview-only until Export | **correct** | `t2_export` is the only writer; it writes `<t2_root>/<selection-id>_<when>/` once and never edits it |
| exports are auditable / tagged | **correct** | every export carries `manifest.json` with inputs, who and when; every run writes a batch record (`runs/batches/`, `runs/_private/`) |
| T2 → Recompute re-runs step 1 for one participant | **NOT built** | `#t2-recompute` is `disabled` in `index.html`; no endpoint, no code path |
| a different epoch size is supported | **NOT built** | step 1 does take `--epoch`, but `tools.run_step1_process` never passes it and `_60s` is hard-coded in 7 places |
| a variant gets a different output filename | **true for free, once wired** | step 1 names its output `<stem>_<epoch_label>.csv` (`preprocess/io.py:39`), so `_30s.csv` cannot collide with `_60s.csv` |
| batch processing picks up new data automatically | **correct** | completeness is recomputed from disk on every grid load, so a new season or file turns the cell orange |
| batch processing picks up a new *program function* | **partly** | a new required artifact or a new device does mark items incomplete; **a changed threshold does not** — see C |
| two actigraphy files for one participant both process | **correct** | `ActigraphProcessor.discover` returns one WorkItem per `.bin`, distinguished by stem in the same folder |
| a single device item can be processed on its own | **NOT built** | `/api/run` accepts only `participants`, `force`, `dry_run`, `initials` |

---

## B. Item-level processing (Panel 1, right side)

### What already exists

`ResolvedSelection` carries `seasons` and `devices`, and `discovery.discover`
already honours both. The core is item-capable. What is missing:

1. `/api/run` drops everything except the participant list (`webapp/server.py:378`).
2. `JobManager.submit` and `Job` carry no season, device or file (`webapp/jobs.py:80`).
3. `force` is all-or-nothing per participant — no way to redo one recording.
4. The Panel-1 grid selects participants only; the right side has no buttons.
5. The right-side verdicts are **transient**: `progressCards` is built from live
   `compliance_result` events and is empty until a run happens. No endpoint
   lists a participant's recordings with their verdicts for Panel 1 —
   `results.output_items` does exactly that, but only Panel 2 uses it.

### Changes

**1 · Item identity.** One recording = `(participant, season, device, stem)`,
where `stem` is the input stem — already what distinguishes two `.bin` files.
Add a `key` property to `models.WorkItem`.

**2 · Selection filter.** Add `items: list[str] | None` to `RunSelection` and
`ResolvedSelection`, plus one filter at the end of `discovery.discover`: when
`items` is set, keep only WorkItems whose `key` is in it. Everything
downstream — skip logic, batching, summaries — is untouched.

**3 · API.** `/api/run` accepts an optional `items` list alongside
`participants`. `force` stays a flag, so "force just this one recording" is
`{items: [...], force: true}` with no 1 GB re-download of the other seasons.

**4 · New read endpoint.** `GET /api/participant/{pid}/items`, returning per
`(season, device, stem)`: `has_input`, `complete`, `missing` (which expected
files are absent), `verdict`, `pct_compliance_mean`, `thresholds_match` (see C).
This is `manifest.participant_status` joined to `results.output_items` on the
stem — both exist, so it is a join, not new logic.

**5 · UI.** Panel 1 right side gains an **Items** box above *Naming check*,
listing that participant's recordings grouped season → device, each row showing
state and verdict with three controls:

- **Process** — only when incomplete (`items:[key]`, `force:false`)
- **Reprocess** — always (`items:[key]`, `force:true`), confirmed for actigraph
  because it re-downloads the `.bin`
- **Open** — reveal the output folder, or jump to the Visualise drill-down

Checkboxes so several rows go in one batch. The existing RUN button and run
modes are unchanged; this is a narrower second entry point into the same
`/api/run`.

**6 · Batch record.** Add `mode: "items"` to `batches.MODES`
(*selected recordings*) and `n_items` to the public record. Item keys contain
participant IDs and filenames, so they go in the **private** record only —
`assert_public_safe` would reject them otherwise, which is the behaviour we
want.

### Cost

Core and API: roughly 120 lines, all additive. UI: one box plus a render
function, roughly 150 lines of `app.js`. Tests: `test_item_selection.py`
covering the discovery filter, the two-`.bin` case, and that a public record
carrying item keys is refused.

---

## C. Staleness after a threshold change

`is_complete` asks only "do the files exist?". Change `min_valid_hours` from 10
to 8 and every cell stays white; a *New and unfinished* run processes nothing.
The only way to apply it is Force reprocess, which also re-downloads every
`.bin`.

The information needed is already on disk: `<stem>_compliance.json` stores
`compliance.parameters` — the exact thresholds that produced it.

**Proposal.** Add `DeviceProcessor.outputs_stale(item, config) -> list[str]`,
default `[]`. For actigraph and mieye, read the compliance JSON and compare its
`parameters` against the config now in force, returning the names of the
parameters that differ. Then:

- the grid gains a **stale** state (blue outline, *thresholds changed*) for a
  cell whose items are all complete but some stale;
- `updateRunPlan` reads *"Processes N new/unfinished and M stale item(s)"*;
- a stale item is reprocessed in *New and unfinished* mode.

This is cheap: recomputing compliance does **not** need the `.bin`. Step 1's
epoch CSV is already in the output folder and `process()` already reuses it, so
a threshold-only reprocess is seconds per item rather than a download.

`OUTPUT_STRUCTURE_VERSION` (`manifest.py:41`) is declared and never read — it is
dead code. Either wire it into the same check (stamp it into the compliance JSON
and treat a mismatch as stale) or delete it. Wiring it is the better option: it
is exactly the "I added a new program function" case.

---

## D. Recompute

The description is right, and the pipeline already supports most of it: re-run
**step 1** with a different setting for one participant, flowing through step 2
→ compliance → a new T2 export, with the variant landing in a differently-named
file so the audit trail holds. Two things make it more than a button.

### D1 · The epoch length is assumed, not passed

`tools.run_step1_process` calls step 1 without `--epoch`, then asserts
`<stem>_60s.csv` exists. `_60s` is hard-coded at:

- `tools.py:172` — the expected output
- `devices/actigraph.py:96,139` — `expected_outputs` and the resume anchor
- `results.py:238` — `item_measures`
- `t2_data.py:171,397` — the globs T2 reads

Threading an epoch label through is mechanical, but it touches the completeness
model: with two epoch lengths in one folder, "complete" has to mean *complete
for the 60 s baseline*, or the grid turns orange the moment a variant is made.
**Proposal: 60 s stays the baseline and defines completeness; variants are extra
and never change a grid colour.**

### D2 · Variants need a home and a label

Writing `<stem>_30s.csv` beside `<stem>_60s.csv` works, but `output_items`
globs `*_compliance.json` and would then list two recordings for the same `.bin`
with no way to tell which is the baseline. Proposal — a sibling folder with its
own record:

```
<output>/<CDxxx>/<Season>/Actigraph/
    <stem>_60s.csv                        baseline, unchanged
    _variants/
        epoch-30s-20261007-143000-YK/
            variant.json                  what changed, from what, by whom
            <stem>_30s.csv  + step 2 outputs + compliance
```

`variant.json` names the parameter changed, its baseline and new value, the
batch id, the initials and the time. A new batch stage `RECOMP` records it: the
public record carries the parameter names and values (no IDs, no paths), so the
public history shows *that* a 30 s variant was made and with what settings,
while the private record says for whom.

### D3 · Then, and only then, the button

T2's **Recompute…** opens a modal: choose participant-seasons from the current
selection, choose the parameter (step 1 `--epoch`, `--filter` / `--low` /
`--high`, `--summary-mode`; compliance thresholds; the MiEYE light floor), see
what will re-run and roughly how long, enter initials, go. It submits a `RECOMP`
job through the same `JobManager`. When it finishes, the T2 preview offers
*use the variant for these participants*, and Export records per
participant-season which variant fed it, in `manifest.json`.

### What Recompute cannot reach

Step 2's CLI takes only `--measures` (`actigraphy-sleep-metrics/cli.py:84`). Its
internals — the Cole–Kripke threshold in `sri.py`, the non-parametric binning —
are not exposed. So Recompute can vary **step 1 settings** and **the
orchestrator's own compliance thresholds**, but not step-2 parameters, without a
change to step 2.

### Cost

D1 is roughly 60 lines, but spread across six files and the completeness model.
D2 is roughly 200 lines (`variants.py` plus processor support). D3 is a modal
plus job plumbing, roughly 250 lines. This is the largest of the three and
should be its own release.

---

## E. Devices not yet wired in

`saliva`, `cognitron` and `qualtrics` are registered as `NotImplementedDevice`,
so discovery stays inert and nothing shows as incomplete because of them. When
one is implemented, adding it to `_REGISTRY` makes every participant with that
folder go orange on the next grid load — the intended behaviour, needing no
other change. `expiwell` and `mieye` are live.

---

## Suggested order

1. **B** — item-level run. Smallest, additive, immediately useful, and a
   prerequisite for D (Recompute needs a way to say *just this recording*).
2. **C** — staleness. Small, and it removes the current reason to reach for
   Force reprocess.
3. **D1 + D2** — epoch label and variant folder, no UI. Makes a variant
   producible from the CLI and recorded properly.
4. **D3** — the Recompute modal and the T2 wiring.
