# runs — batch records

Each press of RUN (or CLI run) that writes data is one **batch**.

| Stage | Id | Example |
|---|---|---|
| `PRE` — staging → master (Compliance panel) | `PRE-<yyyymmdd>-<hhmmss>-<initials>` | `PRE-20260917-143200-YK` |
| `T2` — master → T2 (Visualise → T2 → export) | `T2-<yyyymmdd>-<hhmmss>-<initials>-s<seasons>` | `T2-20260917-143200-YK-s24` |

Preprocess batches carry **no season**: one run can cover any mix of seasons.
T2 records are seasonal: `s24` = each participant's 2nd and 4th seasons,
`s123` = 1st to 3rd.

| Folder | In git? | Contains |
|---|---|---|
| `batches/` | **yes — public** | Stripped records: who (initials), when, mode, number of participants, processed/skipped/failed counts per device, naming-flag counts, thresholds in force, tool versions |
| `batches/_archive/` | yes — public | Batches removed from the dashboard list, with who archived them and when |
| `_private/` | **no** (git-ignored) | Full detail per batch: participant IDs, per-item results and verdicts, output folders, every pipeline event |

Public records never contain participant IDs, file or folder paths, filenames,
device serial numbers, recording dates or results. This is checked in code
before a record is written (`cdcompliance/batches.py`, `assert_public_safe`).
Participant IDs stay out until pseudonymous IDs are approved and switched on
(`privacy.pseudonymise`, see `cdcompliance/pseudo_id.py`); existing records can
then be refreshed with pseudonymous IDs.
