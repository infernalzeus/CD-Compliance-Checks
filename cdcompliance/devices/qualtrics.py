"""Qualtrics device processor — the fan-out, wired into the normal pipeline.

Every other device discovers its work inside the season folder it is given.
Qualtrics cannot: one export covers **every participant** for one survey and one
ordinal season, and they live in a central archive of dated upload batches
(``<staging>/Data Archive/Qualtrics``). So ``discover`` looks sideways at that
archive instead of down into ``season_dir``, and turns what it finds into
ordinary WorkItems so the rest of the pipeline — selection, skipping, batch
records, the grid — needs no special case.

One WorkItem per **participant / season / survey**:

===========================  =========================================
item                         where it is written
===========================  =========================================
seasonal, retrospective      <output>/<pid>/<Season>/Qualtrics/<survey>.csv
enrolment, clinical_*        <output>/<pid>/Qualtrics/<survey>.csv
===========================  =========================================

The once-only surveys have no season of their own (checked: CD008 has exactly
one enrolment response, and the handful of participants with two rows have an
abandoned attempt beside a completed one — not a second season). They are
discovered against the participant's **first** season, because discovery is
season-scoped, but they are written at participant level so they never look
seasonal.

``is_complete`` is a **content** check, not an existence check: a season's CSV
is never finished, because a later upload batch can add a response to a season
whose file already exists. See ``cdcompliance/qualtrics_intake.py``.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config import Config, ResolvedSelection
from ..events import EventBus
from ..models import ComplianceResult, ItemResult, WorkItem
from .base import DeviceProcessor


def _qi():
    """``qualtrics_intake`` imported late.

    It reaches t2_data -> results -> manifest -> discovery -> devices, so
    importing it at module scope would close a cycle through this very package.
    """
    from .. import qualtrics_intake
    return qualtrics_intake


def _t2():
    from .. import t2_data
    return t2_data


def _fingerprint(root: Path) -> tuple[int, float]:
    """Cheap signature of the archive: how many CSVs, and the newest mtime.

    Reading 112 exports to answer "is CD011 done?" for every season of every
    participant in a run would be absurd, so the collected responses are cached.
    A new upload batch changes both halves of this, so the cache cannot go
    stale behind a new delivery.
    """
    count, newest = 0, 0.0
    if not root.exists():
        return 0, 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.lower().endswith(".csv"):
                continue
            count += 1
            try:
                newest = max(newest, os.stat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return count, newest


class QualtricsProcessor(DeviceProcessor):
    name = "qualtrics"
    folder_name = "Qualtrics"

    def __init__(self) -> None:
        self._key: Optional[tuple[str, int, float]] = None
        self._scan = None                      # qi.ArchiveScan
        self._responses: dict[str, list] = {}  # pid -> [qi.Response]

    # ---- archive access ------------------------------------------------------
    def _archive(self, config: Config):
        qi = _qi()
        root = qi.archive_root(config)
        key = (str(root),) + _fingerprint(root)
        if key != self._key:
            self._key = key
            self._scan = qi.scan(config, root)
            self._responses = {}
        return self._scan or qi.ArchiveScan()

    def _for(self, config: Config, participant: str) -> list:
        qi = _qi()
        found = self._archive(config)
        pid = participant.strip().upper()
        if pid not in self._responses:
            chosen = qi.one_per_slot(qi.collect(found, participants=[pid]))
            self._responses[pid] = sorted(chosen.values(),
                                          key=lambda r: (r.survey, r.season or 0))
        return self._responses[pid]

    def reset(self) -> None:
        """Drop the cache. Used after an assimilate so the grid re-reads."""
        self._key, self._scan, self._responses = None, None, {}

    # ---- discovery -----------------------------------------------------------
    def _output_dir(self, config: Config, participant: str,
                    season: Optional[str]) -> Path:
        return _qi().target_dir(config, participant, season)

    def discover(self, config, participant, season, season_dir):
        qi, t2_data = _qi(), _t2()
        responses = self._for(config, participant)
        if not responses:
            return []
        numbers = t2_data.season_numbers(config, participant)
        by_ordinal = {info.get("n"): name for name, info in numbers.items()}
        norm = t2_data._norm_season
        first_season = by_ordinal.get(1)

        items: list[WorkItem] = []
        for r in responses:
            if r.survey in qi.ONCE_SURVEYS:
                # No season of its own. Pin it to the first season so it is
                # discovered exactly once, and write it at participant level.
                if first_season is None or norm(season) != norm(first_season):
                    continue
                out = self._output_dir(config, participant, None)
            else:
                folder = by_ordinal.get(r.season)
                if folder is None or norm(season) != norm(folder):
                    continue
                out = self._output_dir(config, participant, folder)
            items.append(WorkItem(
                participant=participant, season=season, device=self.name,
                source_dir=qi.archive_root(config),
                # The export the response was read from. There is no
                # per-participant input file, and naming a real file keeps the
                # item honest about where its data came from.
                input_path=r.source.path,
                output_dir=out,
                extra={"survey": r.survey, "ordinal": r.season,
                       "response_id": r.response_id,
                       "recorded": r.recorded.isoformat() if r.recorded else None,
                       "batch": r.source.batch},
            ))
        return items

    # ---- master output structure ---------------------------------------------
    def target_path(self, item: WorkItem) -> Optional[Path]:
        survey = item.extra.get("survey")
        if not survey or item.output_dir is None:
            return None
        return Path(item.output_dir) / f"{survey}.csv"

    def expected_outputs(self, item: WorkItem) -> list[Path]:
        path = self.target_path(item)
        return [path] if path else []

    def missing_outputs(self, item: WorkItem) -> list[Path]:
        return [p for p in self.expected_outputs(item) if not p.exists()]

    def is_complete(self, item: WorkItem, config: Config) -> bool:
        """Is THIS response already in the file — not merely: does the file exist.

        A Qualtrics file for one season is never finished. A later upload batch
        can add a response to a season whose CSV was written weeks ago, so the
        only honest answer compares contents.
        """
        path = self.target_path(item)
        rid = item.extra.get("response_id")
        if path is None or not rid:
            return True                      # nothing identifiable to do
        if not path.exists():
            return False
        _, existing = _qi()._read_existing(path)
        return rid in existing

    # ---- processing ----------------------------------------------------------
    def process(self, item, config, selection, bus) -> ItemResult:
        qi = _qi()
        started = datetime.now()
        path = self.target_path(item)
        survey = item.extra.get("survey")
        rid = item.extra.get("response_id")
        if path is None or not survey or not rid:
            return ItemResult(item=item, status="skipped", output_dir=item.output_dir,
                              started_at=started, finished_at=datetime.now(),
                              error="no identifiable survey response")

        bus.emit({"type": "log", "level": "info",
                  "message": f"{item.label}: {survey} response from {item.extra.get('batch')}"})

        # Re-read from the archive rather than trusting a cached row: a run can
        # be minutes after discovery, and the merge has to see every batch.
        found = self._archive(config)
        chosen = qi.one_per_slot(qi.collect(found, surveys=[survey],
                                            participants=[item.participant]))
        rows = [r.row for r in chosen.values() if r.response_id == rid]
        if not rows:
            return ItemResult(item=item, status="skipped", output_dir=item.output_dir,
                              started_at=started, finished_at=datetime.now(),
                              error="response no longer present in the archive")

        if selection.dry_run:
            out = qi.write_rows(path, rows, dry_run=True)
            bus.emit({"type": "log", "level": "info",
                      "message": f"  would add {out['added']}, update {out['updated']}"})
            return ItemResult(item=item, status="skipped", output_dir=item.output_dir,
                              started_at=started, finished_at=datetime.now())

        out = qi.write_rows(path, rows, dry_run=False)
        self.reset()                       # the file changed; the cache has not
        row = rows[0]
        finished = str(row.get("Finished", "")).strip().lower() in {"true", "1", "yes"}
        try:
            progress = int(float(row.get("Progress") or 0))
        except (TypeError, ValueError):
            progress = 0
        compliance = ComplianceResult(
            device=self.name,
            # A survey's compliance is whether the participant finished it.
            # Nothing is computed from the answers here - that is analysis.
            verdict="PASS" if finished else "REVIEW",
            summary={"survey": survey, "ordinal": item.extra.get("ordinal"),
                     "recorded": item.extra.get("recorded"),
                     "finished": finished, "progress": progress,
                     "responses_in_file": out["total"]},
            notes=([] if finished else
                   [f"response is {progress}% complete and was not submitted"]),
        )
        return ItemResult(item=item, status="done", output_dir=item.output_dir,
                          compliance=compliance, artifacts=[path],
                          started_at=started, finished_at=datetime.now())
