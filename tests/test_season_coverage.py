"""Coverage is judged season by season, not once over the whole folder.

Judged once, a participant with Actigraph in one season and MiEYE in another has
every expected device "present" and reads as complete data - while in truth both
seasons have a hole in them. That is how CD011's winter folder, whose MiEYE
folder holds a screenshot and nothing else, passed a ready check with no flag
raised anywhere on the right-hand panel.

Synthetic folders in a temp directory; nothing here reads the study drive.

    python -m unittest tests.test_season_coverage -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datetime import date                            # noqa: E402
from cdcompliance import t2_data                     # noqa: E402
from cdcompliance.config import load_config          # noqa: E402
from cdcompliance.manifest import participant_status, season_board  # noqa: E402

CONFIG = """
paths:
  source_root: {staging}
  output_root: {master}
  t2_root: {t2}
runs_dir: {runs}
devices: [actigraph, mieye]
"""


def _actigraph(season_dir: Path) -> None:
    d = season_dir / "Actigraph"
    d.mkdir(parents=True, exist_ok=True)
    (d / "CD9_left wrist_109248_2025-10-02 10-42-59.bin").write_bytes(b"\0" * 512)


def _mieye(season_dir: Path, *, with_data: bool) -> None:
    d = season_dir / "MiEYE"
    d.mkdir(parents=True, exist_ok=True)
    if with_data:
        (d / "CD9-2025-10-02 11-22-46-logged.csv").write_text("t,lux\n", encoding="utf-8")
    else:
        # What the real gap looks like: the folder is there, and holds a photo.
        (d / "MiEye screenshot.png").write_bytes(b"\x89PNG\r\n")


class SeasonCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="cdcc-cover-"))
        staging, master = cls.tmp / "staging", cls.tmp / "master"
        # CD910: both devices in season 1, only the actigraph in season 2.
        _actigraph(staging / "CD910" / "Autumn  2025")
        _mieye(staging / "CD910" / "Autumn  2025", with_data=True)
        _actigraph(staging / "CD910" / "Winter 2026")
        _mieye(staging / "CD910" / "Winter 2026", with_data=False)
        # CD911: both devices in its one season - genuinely complete coverage.
        _actigraph(staging / "CD911" / "Autumn  2025")
        _mieye(staging / "CD911" / "Autumn  2025", with_data=True)
        # CD912: a season folder that exists and holds nothing at all.
        _actigraph(staging / "CD912" / "Autumn  2025")
        _mieye(staging / "CD912" / "Autumn  2025", with_data=True)
        (staging / "CD912" / "Winter 2026" / "Actigraph").mkdir(parents=True)
        (staging / "CD912" / "Winter 2026" / "MiEYE").mkdir(parents=True)
        master.mkdir(parents=True, exist_ok=True)
        cfg = cls.tmp / "config.yaml"
        cfg.write_text(CONFIG.format(
            staging=staging.as_posix(), master=master.as_posix(),
            t2=(cls.tmp / "t2").as_posix(), runs=(cls.tmp / "runs").as_posix()),
            encoding="utf-8")
        cls.config = load_config(cfg)
        cls.devices = ["actigraph", "mieye"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_hole_in_one_season_is_reported_without_recolouring_the_cell(self) -> None:
        """The gap is found, and it is NOT what the left-hand colour reports.

        The colour answers "can this folder be processed"; a season short of one
        device is the right-hand panel's question, and turning the whole cell
        amber for it was wrong.
        """
        st = participant_status(self.config, "CD910", self.devices)
        self.assertEqual(st.devices_present, ["actigraph", "mieye"])
        self.assertEqual(st.devices_missing, [])
        self.assertEqual(st.data_state, "full")          # the folder has both
        self.assertEqual([g["season"] for g in st.season_gaps], ["Winter 2026"])
        self.assertEqual(st.season_gaps[0]["missing"], ["mieye"])
        self.assertFalse(st.season_gaps[0]["empty"])

    def test_a_survey_response_alone_does_not_start_a_season(self) -> None:
        """Qualtrics lives in a central archive, not the participant's folder.

        Counting it as "this season has data" marked 134 untouched folders as
        short of something, when all that existed was a survey response.
        """
        from cdcompliance.devices import get_processor
        self.assertTrue(get_processor("qualtrics").external_source)
        self.assertFalse(get_processor("actigraph").external_source)

    def test_the_marks_under_the_number_are_seasons_not_devices(self) -> None:
        """Four marks, always four, whatever devices the study adds."""
        st = participant_status(self.config, "CD910", self.devices)
        self.assertEqual(len(st.season_slots), 4)
        self.assertEqual(st.n_season_slots, 4)
        self.assertEqual([s["data"] for s in st.season_slots],
                         ["full", "partial", "absent", "absent"])
        self.assertEqual(st.season_slots[1]["missing"], ["mieye"])
        # A slot with no season folder is "absent", which is not the same as a
        # season folder that exists and holds nothing.
        empty = participant_status(self.config, "CD912", self.devices)
        self.assertEqual([s["data"] for s in empty.season_slots],
                         ["full", "none", "absent", "absent"])

    def test_every_device_in_every_season_is_still_full(self) -> None:
        st = participant_status(self.config, "CD911", self.devices)
        self.assertEqual(st.data_state, "full")
        self.assertEqual(st.season_gaps, [])

    def test_an_empty_season_folder_is_marked_empty_not_merely_short(self) -> None:
        st = participant_status(self.config, "CD912", self.devices)
        gap = next(g for g in st.season_gaps if g["season"] == "Winter 2026")
        self.assertTrue(gap["empty"])
        self.assertEqual(gap["missing"], ["actigraph", "mieye"])

    def test_the_board_names_the_devices_missing_from_each_season(self) -> None:
        board = season_board(self.config, "CD910", self.devices)
        gaps = {s["season"]: s["gaps"] for s in board["seasons"]}
        self.assertEqual(gaps["Autumn  2025"], [])
        self.assertEqual(gaps["Winter 2026"], ["mieye"])

    def test_the_gap_survives_processing(self) -> None:
        """The point of the flag: a season can be fully processed and still short.

        Processing state and data coverage are different questions, and the
        season light only ever answered the first one.
        """
        st = participant_status(self.config, "CD910", self.devices)
        self.assertTrue(st.season_gaps, "a gap must not depend on anything being run")


class SeasonLabels(unittest.TestCase):
    """The study's seasons are calendar quarters beginning on 1 January.

    Winter is January to March of the year the folder names - there is no
    "Winter 2025/26", because a folder cannot be named that way. Checked against
    the drive: across 141 season folders holding dated files, the earliest date
    lands in its folder's quarter in about nine cases out of ten, and the year on
    the folder matches the year in the dates in 135 of 141.

    CD920 here is the shape CD011 has: an autumn visit whose wearable started in
    December and a winter visit in February. Under the old astronomical rule both
    resolved to one winter and the board flagged them as a collision. They are two
    plain seasons, and nothing should be flagged.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="cdcc-label-"))
        staging, master = cls.tmp / "staging", cls.tmp / "master"
        for pid, seasons in {
            # a late autumn visit and a winter one: NOT the same season
            "CD920": (("Autumn  2025", "2025-12-02"), ("Winter 2026", "2026-02-17")),
            # the real collision: two folders holding the same August recording
            "CD921": (("Summer 2026", "2026-08-11"), ("Autumn 2026", "2026-08-11")),
            # the folder's YEAR is wrong, which the season alone would miss
            "CD922": (("Winter 2025", "2026-02-09"),),
        }.items():
            for season, day in seasons:
                d = staging / pid / season / "Actigraph"
                d.mkdir(parents=True)
                (d / f"{pid}_left wrist_109248_{day} 10-42-59.bin").write_bytes(b"\0" * 256)
        master.mkdir(parents=True, exist_ok=True)
        cfg = cls.tmp / "config.yaml"
        cfg.write_text(CONFIG.format(
            staging=staging.as_posix(), master=master.as_posix(),
            t2=(cls.tmp / "t2").as_posix(), runs=(cls.tmp / "runs").as_posix()),
            encoding="utf-8")
        cls.config = load_config(cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def board(self, pid):
        return season_board(self.config, pid, ["actigraph"])

    def test_a_season_never_crosses_a_new_year(self) -> None:
        self.assertEqual(t2_data.calendar_season(date(2026, 1, 9)), ("Winter", 2026))
        self.assertEqual(t2_data.calendar_season(date(2025, 12, 2)), ("Autumn", 2025))
        self.assertEqual(t2_data.season_label("Winter", 2026), "Winter 2026")

    def test_the_label_is_what_the_study_calls_the_folder(self) -> None:
        labels = [s["label"] for s in self.board("CD920")["seasons"]]
        self.assertEqual(labels, ["Autumn 2025", "Winter 2026"])

    def test_a_december_autumn_visit_is_not_a_winter_one(self) -> None:
        """The case that produced the false "T2 would merge these" on CD011."""
        by = {s["label"]: s for s in self.board("CD920")["seasons"]}
        self.assertEqual(by["Autumn 2025"]["calendar_label"], "Autumn 2025")
        self.assertEqual(by["Winter 2026"]["calendar_label"], "Winter 2026")
        for s in self.board("CD920")["seasons"]:
            self.assertFalse(s["duplicate_key"], s["label"])
            self.assertFalse(s["label_conflict"], s["label"])

    def test_two_folders_holding_the_same_season_are_flagged(self) -> None:
        """CD267's shape: one visit's files sitting in two season folders.

        T2 groups participants by calendar season, so it would have to treat
        both as one - which is why this one stays a flag on the cell.
        """
        for s in self.board("CD921")["seasons"]:
            self.assertTrue(s["duplicate_key"], s["label"])

    def test_a_wrong_year_on_the_folder_is_caught_too(self) -> None:
        """CD008: a folder called Winter 2025 holding February 2026."""
        s = self.board("CD922")["seasons"][0]
        self.assertEqual(s["label"], "Winter 2025")
        self.assertEqual(s["calendar_label"], "Winter 2026")
        self.assertTrue(s["label_conflict"])

    def test_the_order_comes_from_the_data_not_the_folder_name(self) -> None:
        """A folder's name can be wrong; the recordings in it cannot."""
        seasons = self.board("CD920")["seasons"]
        self.assertEqual([s["n"] for s in seasons], [1, 2])
        self.assertEqual(seasons[0]["label"], "Autumn 2025")


if __name__ == "__main__":
    unittest.main(verbosity=2)
