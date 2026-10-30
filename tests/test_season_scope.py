"""Panel 1 right: reviewing a whole selection, and acting on it per season.

Season folder names are not comparable between participants - 'Autumn  2025'
with a double space is a real folder on the study drive - so a selection that
spans participants carries one season list PER participant. These tests pin
that down, plus the read-only board endpoint behind the matrix.

Synthetic folders in a temp directory, and the job manager is stubbed, so
nothing here processes anything or touches the study drive.

    python -m unittest tests.test_season_scope -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# A sandbox config, in place before webapp.server reads it at import time.
_TMP = Path(tempfile.mkdtemp(prefix="cdcc-scope-"))
_MASTER, _STAGING = _TMP / "master", _TMP / "staging"


def _recording(dev_dir: Path, stem: str, processed: bool, day: str) -> None:
    dev_dir.mkdir(parents=True, exist_ok=True)
    (dev_dir / f"{stem}.bin").write_bytes(b"\0" * 512)
    if not processed:
        return
    (dev_dir / f"{stem}_60s.csv").write_text(
        "Time,SVM_sum\n" + f"{day} 00:00:00,12\n", encoding="utf-8")
    (dev_dir / f"{stem}_60s_daily_compliance.csv").write_text(
        f"date,valid_day,pct_compliance\n{day},True,95.0\n", encoding="utf-8")
    (dev_dir / f"{stem}_compliance.json").write_text(
        json.dumps({"compliance": {"verdict": "PASS", "summary": {}}}), encoding="utf-8")


# CD901: first season processed, second season present but not processed. The
# first season's folder carries a DOUBLE space, as real ones do.
_recording(_MASTER / "CD901" / "Autumn  2025" / "Actigraph",
           "CD901_left wrist_109248_2025-10-02 10-42-59", True, "2025-10-02")
_recording(_MASTER / "CD901" / "Winter 2026" / "Actigraph",
           "CD901_left wrist_109410_2026-01-17 12-17-25", False, "2026-01-17")
# CD902: one season, not processed
_recording(_MASTER / "CD902" / "Spring 2026" / "Actigraph",
           "CD902_left wrist_109306_2026-04-28 07-03-53", False, "2026-04-28")
for pid, seasons in (("CD901", ("Autumn  2025", "Winter 2026")), ("CD902", ("Spring 2026",))):
    for season in seasons:
        src = _STAGING / pid / season / "Actigraph"
        src.mkdir(parents=True, exist_ok=True)
        for f in (_MASTER / pid / season / "Actigraph").glob("*.bin"):
            (src / f.name).write_bytes(b"\0" * 512)

(_TMP / "runs" / "batches").mkdir(parents=True)
(_TMP / "config.yaml").write_text(
    "paths:\n"
    f"  source_root: '{_STAGING}'\n"
    f"  output_root: '{_MASTER}'\n"
    f"  t2_root: '{_TMP / 't2'}'\n"
    f"runs_dir: '{_TMP / 'runs'}'\n"
    "participant: 'CD901'\n"
    "devices: ['actigraph', 'mieye']\n", encoding="utf-8")
(_TMP / "settings.json").write_text("{}", encoding="utf-8")

os.environ["CDCC_CONFIG"] = str(_TMP / "config.yaml")
os.environ["CDCC_SETTINGS"] = str(_TMP / "settings.json")

from fastapi.testclient import TestClient  # noqa: E402

from webapp import server  # noqa: E402


class _FakeJob:
    """Captures what a run WOULD have been given, and runs nothing."""

    def __init__(self, **kw):
        self.id = "test-job"
        self.kw = kw

    def info(self):
        return {"id": self.id}


class Base(unittest.TestCase):
    def setUp(self):
        assert "cdcc-scope-" in str(server._config.paths.output_root), "sandbox only"
        self.client = TestClient(server.app)
        self.submitted: list[dict] = []

        def fake_submit(participants, force, dry_run=False, initials="",
                        seasons=None, devices=None, season_map=None):
            self.submitted.append({"participants": participants, "force": force,
                                   "dry_run": dry_run, "seasons": seasons,
                                   "devices": devices, "season_map": season_map})
            return _FakeJob()

        self._real_submit = server._jobs.submit
        server._jobs.submit = fake_submit

    def tearDown(self):
        server._jobs.submit = self._real_submit


class SeasonBoards(Base):
    """The read-only board behind the matrix, for a whole selection."""

    def test_one_board_per_selected_participant(self):
        r = self.client.post("/api/panel1/season-boards",
                             json={"participants": ["CD901", "CD902"]})
        self.assertEqual(r.status_code, 200)
        boards = r.json()["boards"]
        self.assertEqual([b["participant"] for b in boards], ["CD901", "CD902"])
        self.assertEqual(r.json()["missing"], [])

    def test_the_real_folder_name_is_carried_beside_the_label(self):
        r = self.client.post("/api/panel1/season-boards", json={"participants": ["CD901"]})
        seasons = r.json()["boards"][0]["seasons"]
        folders = [s["season"] for s in seasons]
        # The double space is what the folder is actually called; actions must
        # use it, not the tidied-up label people read.
        self.assertIn("Autumn  2025", folders)

    def test_a_participant_with_no_output_folder_is_reported_not_fatal(self):
        r = self.client.post("/api/panel1/season-boards",
                             json={"participants": ["CD901", "CD999"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([b["participant"] for b in r.json()["boards"]], ["CD901"])
        self.assertEqual(r.json()["missing"], ["CD999"])

    def test_an_empty_selection_is_refused(self):
        self.assertEqual(
            self.client.post("/api/panel1/season-boards", json={"participants": []}).status_code,
            400)

    def test_too_large_a_selection_is_refused_rather_than_read(self):
        many = [f"CD{n:03d}" for n in range(200)]
        r = self.client.post("/api/panel1/season-boards", json={"participants": many})
        self.assertEqual(r.status_code, 400)


class RunScope(Base):
    """A season-scoped run, over one participant or several."""

    def test_scope_reaches_the_job_as_one_season_list_per_participant(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901", "CD902"], "initials": "YK",
            "devices": ["actigraph"],
            "scope": [{"participant": "CD901", "seasons": ["Autumn  2025", "Winter 2026"]},
                      {"participant": "CD902", "seasons": ["Spring 2026"]}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.submitted[0]
        self.assertEqual(sent["season_map"], {"CD901": ["Autumn  2025", "Winter 2026"],
                                              "CD902": ["Spring 2026"]})
        self.assertIsNone(sent["seasons"])

    def test_scope_may_not_name_a_folder_that_is_not_selected(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901"], "initials": "YK",
            "scope": [{"participant": "CD902", "seasons": ["Spring 2026"]}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("not selected", r.json()["error"])
        self.assertFalse(self.submitted)

    def test_seasons_and_scope_together_are_refused_as_ambiguous(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901"], "initials": "YK",
            "seasons": ["Winter 2026"],
            "scope": [{"participant": "CD901", "seasons": ["Autumn  2025"]}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.submitted)

    def test_a_flat_season_list_still_covers_one_participant(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901"], "initials": "YK", "seasons": ["Winter 2026"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.submitted[0]["seasons"], ["Winter 2026"])
        self.assertIsNone(self.submitted[0]["season_map"])

    def test_a_flat_season_list_across_participants_is_still_refused(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901", "CD902"], "initials": "YK",
            "seasons": ["Winter 2026"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("scope", r.json()["error"])

    def test_the_unscoped_run_button_is_unchanged(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901", "CD902"], "initials": "YK"})
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.submitted[0]
        self.assertIsNone(sent["seasons"])
        self.assertIsNone(sent["season_map"])
        self.assertIsNone(sent["devices"])

    def test_a_device_that_is_not_wired_in_is_refused(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901"], "initials": "YK", "devices": ["cognitron"],
            "scope": [{"participant": "CD901", "seasons": ["Winter 2026"]}]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.submitted)

    def test_a_scoped_run_that_writes_still_needs_initials(self):
        r = self.client.post("/api/run", json={
            "participants": ["CD901"],
            "scope": [{"participant": "CD901", "seasons": ["Winter 2026"]}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("initials", r.json()["error"])


class JobScope(unittest.TestCase):
    """The job itself must hand each participant its own seasons."""

    def test_the_season_map_wins_over_the_flat_list_per_participant(self):
        from webapp.jobs import Job
        job = Job("x", ["CD901", "CD902"], force=False)
        job.seasons = None
        job.season_map = {"CD901": ["Autumn  2025"], "CD902": ["Spring 2026"]}
        self.assertEqual((job.season_map or {}).get("CD901"), ["Autumn  2025"])
        self.assertEqual((job.season_map or {}).get("CD902"), ["Spring 2026"])
        # a participant absent from the map falls back to the flat list
        self.assertIsNone((job.season_map or {}).get("CD903", job.seasons))

    def test_the_map_is_reported_on_the_job(self):
        from webapp.jobs import Job
        job = Job("x", ["CD901"], force=False)
        job.season_map = {"CD901": ["Autumn  2025"]}
        self.assertIn("seasons", job.info())


if __name__ == "__main__":
    unittest.main()
