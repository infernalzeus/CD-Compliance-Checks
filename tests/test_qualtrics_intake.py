"""Qualtrics intake: append, dedup, season mapping, content check.

Synthetic exports in a temp directory - never the study archive, which is
read-only to this project and lives on OneDrive where opening a file hydrates
it. The shapes here are copied from what the real archive was observed to
contain on 2026-10-30.

    python -m unittest tests.test_qualtrics_intake -v
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import qualtrics_intake as qi  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402

HEADER = ["StartDate", "RecordedDate", "ResponseId", "Finished", "Progress", "CHIPDID", "Q1"]


def export(folder: Path, name: str, rows: list[list[str]]) -> Path:
    """One Qualtrics CSV, including the two extra header rows it always writes."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerow([f"{c} text" for c in HEADER])
        w.writerow(['{"ImportId":"x"}'] * len(HEADER))
        w.writerows(rows)
    return path


def row(rid: str, pid: str, recorded: str, finished: str = "True",
        progress: str = "100", answer: str = "a") -> list[str]:
    return [recorded, recorded, rid, finished, progress, pid, answer]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(self.tmp / "missing.yaml")
        self.cfg.paths.source_root = self.tmp / "staging"
        self.cfg.paths.output_root = self.tmp / "master"
        self.archive = self.cfg.paths.source_root / qi.ARCHIVE_SUBDIR
        self.archive.mkdir(parents=True)

    def season_folders(self, pid: str, *names: str) -> None:
        """Give a participant season folders with dated recordings in them.

        Season ordinals come from the recordings, so a folder on its own is not
        enough - there has to be something dated inside it.
        """
        for name in names:
            dev = self.cfg.paths.output_root / pid / name / "Actigraph"
            dev.mkdir(parents=True, exist_ok=True)
            # the date in the filename is what season_numbers reads
            stamp = {"Autumn 2025": "2025-10-02", "Winter 2026": "2026-01-17",
                     "Spring 2026": "2026-04-28", "Summer 2026": "2026-07-21"}[name.strip()]
            (dev / f"{pid}_left wrist_109248_{stamp} 10-42-59.bin").write_bytes(b"\0" * 16)


class Filenames(Base):
    def test_a_season_tagged_export_is_classified(self):
        self.assertEqual(qi.classify("Season 3 CHiP-D CS Seasonal Visit (S3)"),
                         ("seasonal", 3))

    def test_an_untagged_seasonal_export_has_no_season(self):
        self.assertEqual(qi.classify("CHiP-D CS (Seasonal)"), ("seasonal", None))

    def test_once_only_surveys_never_carry_a_season(self):
        for stem, survey in (("CHiP-D Enrolment", "enrolment"),
                             ("CHiP-D Clinical Enrolment", "clinical_enrolment"),
                             ("CHiP-D Clinical Screener CHiP-D (2026)", "clinical_screening")):
            self.assertEqual(qi.classify(stem), (survey, None))

    def test_a_browser_duplicate_suffix_still_parses(self):
        e = qi.parse_export(Path("Season 1 CHiP-D CS Seasonal Visit (S1)_"
                                 "October 7, 2026_18.02 (1).csv"))
        self.assertIsNotNone(e)
        self.assertEqual((e.survey, e.season), ("seasonal", 1))

    def test_scanning_reads_filenames_only(self):
        export(self.archive / "Qualtrics 19-01-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_19 January 2026_11.00.csv",
               [row("R_a", "CD008", "2025-09-25")])
        (self.archive / "Qualtrics 19-01-26" / "notes.csv").write_text("x", encoding="utf-8")
        found = qi.scan(self.cfg)
        self.assertEqual(len(found.exports), 1)
        self.assertEqual([p.name for p in found.unrecognised], ["notes.csv"])


class UntaggedExports(Base):
    """The 18 pre-split exports: flagged, read, and never filed on their own."""

    def setUp(self):
        super().setUp()
        export(self.archive / "Qualtrics 12-01-26",
               "CHiP-D CS (Seasonal)_12 January 2026_11.10.csv",
               [row("R_a", "CD008", "2025-09-25")])
        export(self.archive / "Qualtrics 19-01-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_19 January 2026_11.00.csv",
               [row("R_a", "CD008", "2025-09-25")])

    def test_an_untagged_export_raises_a_warning_not_a_silent_skip(self):
        found = qi.scan(self.cfg)
        self.assertEqual(len(found.untagged), 1)
        warn = [w for w in qi.warnings(found) if w["severity"] == "warning"]
        self.assertEqual(len(warn), 1)
        self.assertIn("no season in the filename", warn[0]["message"])

    def test_its_responses_merge_into_the_tagged_copy_rather_than_duplicating(self):
        got = qi.collect(qi.scan(self.cfg))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].season, 1)

    def test_a_response_that_is_only_ever_untagged_is_not_filed(self):
        export(self.archive / "Qualtrics 12-01-26",
               "CHiP-D Retrospective_12 January 2026_11.11.csv",
               [row("R_orphan", "CD008", "2025-10-13")])
        got = qi.collect(qi.scan(self.cfg), surveys=["retrospective"])
        self.assertEqual(got, [])     # no season of its own - nowhere to put it


class AppendAcrossBatches(Base):
    """One participant-season split across upload batches is the whole point."""

    def test_responses_from_several_batches_are_merged_not_replaced(self):
        export(self.archive / "Qualtrics 09-02-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 February 2026_10.00.csv",
               [row("R_a", "CD008", "2025-09-25")])
        export(self.archive / "Qualtrics 09-03-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 March 2026_10.00.csv",
               [row("R_b", "CD011", "2025-11-05")])
        got = qi.collect(qi.scan(self.cfg))
        self.assertEqual({r.participant for r in got}, {"CD008", "CD011"})

    def test_newest_wins_would_have_lost_the_earlier_batch(self):
        """The failure this whole module exists to avoid, stated as a test."""
        found = self.archive
        export(found / "Qualtrics 09-02-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 February 2026_10.00.csv",
               [row("R_a", "CD008", "2025-09-25")])
        export(found / "Qualtrics 09-03-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 March 2026_10.00.csv",
               [row("R_b", "CD011", "2025-11-05")])
        scanned = qi.scan(self.cfg)
        newest = max((e for e in scanned.exports), key=lambda e: e.exported_at)
        _, rows_newest = qi._read_rows(newest.path)
        self.assertEqual(len(rows_newest), 1)            # newest-wins keeps one
        self.assertEqual(len(qi.collect(scanned)), 2)    # appending keeps both


class Duplicates(Base):
    """Enrolment is where real duplicates live: abandoned attempts."""

    def test_a_finished_response_beats_an_abandoned_one(self):
        export(self.archive / "Qualtrics 07-10-2026",
               "CHiP-D Enrolment_October 7, 2026_18.02.csv",
               [row("R_done", "CD003", "2025-09-02", "True", "100"),
                row("R_part", "CD003", "2025-09-09", "False", "99")])
        best = qi.one_per_slot(qi.collect(qi.scan(self.cfg)))
        self.assertEqual(len(best), 1)
        self.assertEqual(best[("CD003", "enrolment", None)].response_id, "R_done")

    def test_with_none_finished_the_furthest_progress_wins(self):
        export(self.archive / "Qualtrics 07-10-2026",
               "CHiP-D Enrolment_October 7, 2026_18.02.csv",
               [row("R_low", "CD006", "2025-09-07", "False", "34"),
                row("R_high", "CD006", "2025-09-09", "False", "62")])
        best = qi.one_per_slot(qi.collect(qi.scan(self.cfg)))
        self.assertEqual(best[("CD006", "enrolment", None)].response_id, "R_high")


class SeasonMapping(Base):
    def setUp(self):
        super().setUp()
        self.season_folders("CD011", "Autumn 2025", "Winter 2026", "Spring 2026")

    def test_the_ordinal_and_the_response_date_agree(self):
        export(self.archive / "Qualtrics 02-04-26",
               "Season 3 CHiP-D CS Seasonal Visit (S3)_2 April 2026_10.00.csv",
               [row("R_s3", "CD011", "2026-03-12")])
        got = qi.collect(qi.scan(self.cfg))
        aligned = qi.season_alignment(self.cfg, "CD011", got)
        self.assertEqual(len(aligned), 1)
        self.assertTrue(aligned[0].agrees, aligned[0].note)
        self.assertEqual(aligned[0].master_season, "Spring 2026")

    def test_a_response_recorded_in_a_different_season_is_reported_not_resolved(self):
        # "S3" but recorded in autumn: the ordinal and the date disagree
        export(self.archive / "Qualtrics 02-04-26",
               "Season 3 CHiP-D CS Seasonal Visit (S3)_2 April 2026_10.00.csv",
               [row("R_odd", "CD011", "2025-10-20")])
        aligned = qi.season_alignment(self.cfg, "CD011", qi.collect(qi.scan(self.cfg)))
        self.assertFalse(aligned[0].agrees)
        self.assertIn("different calendar season", aligned[0].note)

    def test_an_ordinal_with_no_folder_yet_is_reported(self):
        export(self.archive / "Qualtrics 07-07-26",
               "Season 4 CHiP-D CS Seasonal Visit (S4)_7 July 2026_10.00.csv",
               [row("R_s4", "CD011", "2026-06-11")])
        aligned = qi.season_alignment(self.cfg, "CD011", qi.collect(qi.scan(self.cfg)))
        self.assertFalse(aligned[0].agrees)
        self.assertIn("no season folder", aligned[0].note)


class Writing(Base):
    def setUp(self):
        super().setUp()
        self.season_folders("CD011", "Autumn 2025", "Winter 2026")
        export(self.archive / "Qualtrics 09-02-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 February 2026_10.00.csv",
               [row("R_a", "CD011", "2025-11-05")])
        export(self.archive / "Qualtrics 09-02-26",
               "CHiP-D Enrolment_9 February 2026_10.01.csv",
               [row("R_e", "CD011", "2025-09-28")])

    def test_a_seasonal_survey_lands_beside_the_other_devices(self):
        qi.assimilate(self.cfg)
        path = (self.cfg.paths.output_root / "CD011" / "Autumn 2025"
                / qi.DEVICE_DIRNAME / "seasonal.csv")
        self.assertTrue(path.exists())

    def test_a_once_only_survey_sits_at_participant_level_not_in_a_season(self):
        qi.assimilate(self.cfg)
        path = self.cfg.paths.output_root / "CD011" / qi.DEVICE_DIRNAME / "enrolment.csv"
        self.assertTrue(path.exists(), "enrolment must not be filed under a season")

    def test_a_dry_run_writes_nothing_but_reports_what_it_would(self):
        out = qi.assimilate(self.cfg, dry_run=True)
        self.assertEqual(out["added"], 2)
        self.assertFalse((self.cfg.paths.output_root / "CD011" / qi.DEVICE_DIRNAME).exists())

    def test_running_twice_adds_nothing_the_second_time(self):
        first = qi.assimilate(self.cfg)
        second = qi.assimilate(self.cfg)
        self.assertEqual(first["added"], 2)
        self.assertEqual((second["added"], second["updated"]), (0, 0))

    def test_a_later_batch_appends_to_the_file_already_there(self):
        qi.assimilate(self.cfg)
        export(self.archive / "Qualtrics 09-03-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 March 2026_10.00.csv",
               [row("R_a", "CD011", "2025-11-05"),
                row("R_a2", "CD011", "2025-11-06")])
        out = qi.assimilate(self.cfg)
        path = (self.cfg.paths.output_root / "CD011" / "Autumn 2025"
                / qi.DEVICE_DIRNAME / "seasonal.csv")
        with path.open(newline="", encoding="utf-8-sig") as fh:
            ids = {r["ResponseId"] for r in csv.DictReader(fh)}
        self.assertEqual(ids, {"R_a", "R_a2"})
        self.assertEqual(out["added"], 1)


class OddSeasonFolderNames(Base):
    """The normalised season key is not a path - the folder may be double-spaced."""

    def test_the_file_lands_in_the_real_folder_not_a_new_one(self):
        dev = self.cfg.paths.output_root / "CD011" / "Autumn  2025" / "Actigraph"
        dev.mkdir(parents=True)
        (dev / "CD011_left wrist_109248_2025-10-02 10-42-59.bin").write_bytes(b"\0" * 16)
        export(self.archive / "Qualtrics 09-02-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 February 2026_10.00.csv",
               [row("R_a", "CD011", "2025-11-05")])
        qi.assimilate(self.cfg)
        seasons = [p.name for p in (self.cfg.paths.output_root / "CD011").iterdir() if p.is_dir()]
        self.assertEqual(sorted(seasons), ["Autumn  2025"],
                         "a second, differently-spaced season folder was created")


class ContentCheck(Base):
    """Completeness by content, not by whether a file exists."""

    def setUp(self):
        super().setUp()
        self.season_folders("CD011", "Autumn 2025", "Winter 2026")
        export(self.archive / "Qualtrics 09-02-26",
               "Season 1 CHiP-D CS Seasonal Visit (S1)_9 February 2026_10.00.csv",
               [row("R_a", "CD011", "2025-11-05")])

    def test_nothing_assimilated_reads_as_pending(self):
        self.assertEqual(qi.content_check(self.cfg, "CD011")["state"], "pending")

    def test_after_assimilation_it_reads_as_complete(self):
        qi.assimilate(self.cfg)
        self.assertEqual(qi.content_check(self.cfg, "CD011")["state"], "complete")

    def test_a_new_response_for_a_season_already_written_reopens_it(self):
        """An existence check would call this done. It is not done."""
        qi.assimilate(self.cfg)
        export(self.archive / "Qualtrics 09-03-26",
               "Season 2 CHiP-D CS Seasonal Visit (S2)_9 March 2026_10.00.csv",
               [row("R_b", "CD011", "2026-01-22")])
        check = qi.content_check(self.cfg, "CD011")
        self.assertEqual(check["state"], "partial")
        self.assertEqual(check["n_present"], 1)
        self.assertEqual(check["n_expected"], 2)

    def test_the_file_existing_is_not_what_is_being_measured(self):
        qi.assimilate(self.cfg)
        path = (self.cfg.paths.output_root / "CD011" / "Autumn 2025"
                / qi.DEVICE_DIRNAME / "seasonal.csv")
        self.assertTrue(path.exists())
        # same file, a response removed from it: existence unchanged, content not
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
        self.assertTrue(path.exists())
        self.assertEqual(qi.content_check(self.cfg, "CD011")["state"], "pending")


if __name__ == "__main__":
    unittest.main()
