"""The season's assessment log is copied beside the results, verbatim.

Synthetic folders in a temp directory - never the study drive.

    python -m unittest tests.test_season_documents -v
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import pipeline  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402
from cdcompliance.events import EventBus  # noqa: E402
from cdcompliance.models import WorkItem  # noqa: E402

LOG_NAME = "CD901-September-25-Assessment-Log-S1 .xlsx"   # trailing space is real


class SeasonDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(self.tmp / "missing.yaml")
        self.cfg.paths.source_root = self.tmp / "staging"
        self.cfg.paths.output_root = self.tmp / "master"
        self.season_src = self.cfg.paths.source_root / "CD901" / "Autumn 2025"
        self.season_out = self.cfg.paths.output_root / "CD901" / "Autumn 2025"
        (self.season_src / "Actigraph").mkdir(parents=True)
        (self.season_src / "MiEYE").mkdir(parents=True)
        self.log = self.season_src / LOG_NAME
        self.log.write_bytes(b"assessment log v1")
        # paperwork that is NOT an assessment log stays where it is
        (self.season_src / "consent.pdf").write_bytes(b"pdf")
        self.bus = EventBus()
        self.items = [
            WorkItem(participant="CD901", season="Autumn 2025", device=dev,
                     source_dir=self.season_src / folder,
                     input_path=None, output_dir=self.season_out / folder)
            for dev, folder in (("actigraph", "Actigraph"), ("mieye", "MiEYE"))
        ]

    def test_copied_once_per_season_and_only_the_log(self):
        copied = pipeline.copy_season_documents(self.cfg, self.items, self.bus)
        self.assertEqual([p.name for p in copied], [LOG_NAME])      # one copy, not one per device
        dest = self.season_out / LOG_NAME
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"assessment log v1")   # verbatim
        self.assertFalse((self.season_out / "consent.pdf").exists())

    def test_second_run_skips_and_a_newer_log_replaces(self):
        pipeline.copy_season_documents(self.cfg, self.items, self.bus)
        self.assertEqual(pipeline.copy_season_documents(self.cfg, self.items, self.bus), [])
        time.sleep(0.01)
        self.log.write_bytes(b"assessment log v2")
        import os
        os.utime(self.log, (time.time() + 5, time.time() + 5))
        copied = pipeline.copy_season_documents(self.cfg, self.items, self.bus)
        self.assertEqual(len(copied), 1)
        self.assertEqual((self.season_out / LOG_NAME).read_bytes(), b"assessment log v2")

    def test_a_missing_log_is_not_an_error(self):
        self.log.unlink()
        self.assertEqual(pipeline.copy_season_documents(self.cfg, self.items, self.bus), [])

    def test_events_name_the_document(self):
        seen = []
        self.bus.subscribe(lambda e: seen.append(e))
        pipeline.copy_season_documents(self.cfg, self.items, self.bus)
        copied = [e for e in seen if e["type"] == "document_copied"]
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["name"], LOG_NAME)
        self.assertEqual(copied[0]["label"], "CD901 / Autumn 2025")


if __name__ == "__main__":
    unittest.main()
