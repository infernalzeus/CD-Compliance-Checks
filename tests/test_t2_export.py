"""P4 cut-offs and P5 export. Synthetic data in a temp folder only.

    python -m unittest tests.test_t2_export -v
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import batches, t2_data, t2_export, t2_selections, t2_stats  # noqa: E402
from tests.test_t2_p0 import _build_season, _config  # noqa: E402
from tests.test_t2_stats import _frame, _rows  # noqa: E402


class CutoffTests(unittest.TestCase):
    def setUp(self):
        self.long = _frame(
            _rows("CD901", "act_wear_hours", "actigraph", [10, 20, 22, 8, 19]) +
            _rows("CD901", "light_mel_mean", "mieye", [100, 200, 300, 400, 500]))

    def test_a_cutoff_keeps_whole_days(self):
        kept = t2_stats.day_frame(self.long, cutoffs=[{"variable": "act_wear_hours", "op": ">=", "value": 19}])
        # days 2, 3 and 5 qualify; BOTH measures survive for those days
        self.assertEqual(sorted(kept[kept["variable"] == "light_mel_mean"]["value"]), [200, 300, 500])
        self.assertEqual(len(kept), 6)

    def test_cutoffs_stack_and_can_empty_the_frame(self):
        two = t2_stats.day_frame(self.long, cutoffs=[
            {"variable": "act_wear_hours", "op": ">=", "value": 19},
            {"variable": "light_mel_mean", "op": "<=", "value": 250}])
        self.assertEqual(len(two), 2)                                  # only day 2
        none = t2_stats.day_frame(self.long, cutoffs=[{"variable": "act_wear_hours", "op": ">=", "value": 99}])
        self.assertTrue(none.empty)

    def test_a_day_without_that_measure_cannot_qualify(self):
        long = _frame(_rows("CD901", "act_wear_hours", "actigraph", [20, 20]) +
                      _rows("CD902", "light_mel_mean", "mieye", [100, 100]))
        kept = t2_stats.day_frame(long, cutoffs=[{"variable": "act_wear_hours", "op": ">=", "value": 1}])
        self.assertEqual(set(kept["participant"]), {"CD901"})

    def test_bad_rules_are_ignored_not_fatal(self):
        for rule in ({"variable": "act_wear_hours", "op": "~=", "value": 5},
                     {"variable": "nope", "op": ">=", "value": 5},
                     {"variable": "act_wear_hours", "op": ">=", "value": "abc"}):
            out = t2_stats.day_frame(self.long, cutoffs=[rule])
            self.assertEqual(len(out), 0 if rule["variable"] == "nope" else 10)

    def test_ranges_size_the_sliders(self):
        ranges = {r["variable"]: r for r in t2_stats.measure_ranges(self.long)}
        wear = ranges["act_wear_hours"]
        self.assertEqual((wear["min"], wear["max"], wear["median"], wear["n_days"]), (8.0, 22.0, 19.0, 5))


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = _config(cls.tmp)
        cls.cfg.paths.t2_root = cls.tmp / "t2out"
        _build_season(cls.cfg.paths.output_root, "CD901", "Autumn 2025",
                      date(2025, 9, 27), date(2025, 9, 24), date(2025, 9, 24), seed=7)
        _build_season(cls.cfg.paths.output_root, "CD902", "Autumn 2025",
                      date(2025, 9, 27), date(2025, 9, 24), date(2025, 9, 24), seed=8)
        cls.record = t2_selections.create(cls.cfg, {
            "participants": ["CD901", "CD902"], "devices": ["actigraph", "mieye", "expiwell"],
            "initials": "YK", "name": "autumn pilot",
            "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        cls.batch_id = cls.record["batch_id"]
        _, cls.sel = t2_selections.load_selection(cls.cfg, cls.batch_id)

    def _frame(self, **payload):
        frame = t2_selections.frame_for(self.cfg, self.batch_id, self.sel, refresh=True)
        return t2_selections._filtered_frame(frame, t2_selections._filters(payload))

    def test_preview_lists_every_file_before_writing(self):
        out = t2_selections.export_preview(self.cfg, self.batch_id, self.sel, {})
        names = [f["name"] for f in out["files"]]
        self.assertEqual(names, [n for n, _ in t2_export.FILES])
        self.assertTrue(all(f["what"] for f in out["files"]))
        long = next(f for f in out["files"] if f["name"] == "data_long.csv")
        self.assertGreater(long["rows"], 0)
        self.assertGreater(out["inclusion"]["days"], 0)
        self.assertEqual(out["inclusion"]["participants"], 2)
        # a preview writes nothing: no new export folder appears
        before = sorted(p.name for p in self.cfg.paths.t2_root.iterdir()) if self.cfg.paths.t2_root.exists() else []
        t2_selections.export_preview(self.cfg, self.batch_id, self.sel, {})
        after = sorted(p.name for p in self.cfg.paths.t2_root.iterdir()) if self.cfg.paths.t2_root.exists() else []
        self.assertEqual(before, after)

    def test_export_writes_the_files_and_records_the_batch(self):
        result = t2_selections.export_write(self.cfg, self.batch_id, self.sel,
                                            {"approver": "yk", "days": [1, 15]})
        dest = Path(result["destination"])
        self.assertTrue(dest.is_dir())
        # every file is written; "reports/" only appears when the tools made PDFs
        expected = sorted(n for n, _ in t2_export.FILES if not n.endswith("/"))
        self.assertEqual(sorted(p.name for p in dest.iterdir() if p.is_file()), expected)
        # one timestamp in the folder name, not two
        self.assertRegex(dest.name, r"^T2-\d{8}-\d{6}-")
        self.assertEqual(len(re.findall(r"\d{8}-\d{6}", dest.name)), 1)
        self.assertEqual(result["approved_by"], "YK")

        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["approved_by"], "YK")
        self.assertEqual(manifest["selection_id"], self.batch_id)
        self.assertEqual(list(manifest["inclusion"]["day_range"]), [1, 15])
        self.assertTrue(manifest["source_files"])
        self.assertTrue(all(len(f["sha256"]) == 64 for f in manifest["source_files"]))
        self.assertIn("cd-compliance-checks", manifest["tool_versions"])

        header = (dest / "data_long.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("night_of", header)
        self.assertIn("window_day", header)
        self.assertNotIn("recording", header)         # file stems never leave the machine

        exports = [b for b in batches.list_batches(self.cfg, stage="T2") if b.get("mode") == "export"]
        self.assertEqual(len(exports), 1)
        public = json.dumps(exports[0])
        for leak in ("CD901", "CD902", str(self.tmp)):
            self.assertNotIn(leak, public)

    def test_no_approver_and_nothing_to_export_are_refused(self):
        with self.assertRaises(ValueError):
            t2_selections.export_write(self.cfg, self.batch_id, self.sel, {"approver": ""})
        with self.assertRaises(ValueError):
            t2_selections.export_write(self.cfg, self.batch_id, self.sel,
                                       {"approver": "YK", "days": [400, 401]})
        # a refused export leaves nothing half-written behind
        if self.cfg.paths.t2_root.exists():
            self.assertEqual([p.name for p in self.cfg.paths.t2_root.iterdir()
                              if p.name.startswith(".")], [])

    def test_a_cutoff_narrows_what_would_be_exported(self):
        wide = t2_selections.export_preview(self.cfg, self.batch_id, self.sel, {})
        narrow = t2_selections.export_preview(self.cfg, self.batch_id, self.sel, {
            "cutoffs": [{"variable": "act_wear_hours", "op": ">=", "value": 21}]})
        self.assertLess(narrow["inclusion"]["days"], wide["inclusion"]["days"])
        self.assertEqual(narrow["inclusion"]["cut_offs"][0]["variable"], "act_wear_hours")


if __name__ == "__main__":
    unittest.main()


class ReportScopeTests(unittest.TestCase):
    """Device report PDFs follow the export, not the whole output tree."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = _config(cls.tmp)
        cls.cfg.paths.t2_root = cls.tmp / "t2out"
        for season, act in (("Autumn 2025", date(2025, 9, 27)), ("Winter 2025", date(2026, 1, 20))):
            _build_season(cls.cfg.paths.output_root, "CD901", season, act,
                          act - timedelta(days=3), act - timedelta(days=3), seed=11)
            pdf = (cls.cfg.paths.output_root / "CD901" / season / "Actigraph" / f"CD901_{season}_report.pdf")
            pdf.write_bytes(b"%PDF-1.4 report")
        cls.record = t2_selections.create(cls.cfg, {
            "participants": ["CD901"], "devices": ["actigraph", "mieye", "expiwell"],
            "seasons": ["2025-autumn"], "initials": "YK",
            "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        _, cls.sel = t2_selections.load_selection(cls.cfg, cls.record["batch_id"])

    def test_only_the_exported_season_ships_its_reports(self):
        result = t2_selections.export_write(self.cfg, self.record["batch_id"], self.sel,
                                            {"approver": "YK"})
        dest = Path(result["destination"])
        pdfs = sorted(p.relative_to(dest).as_posix() for p in (dest / "reports").rglob("*.pdf"))
        self.assertEqual(pdfs, ["reports/CD901/Autumn 2025/CD901_Autumn 2025_report.pdf"])
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["device_reports"]), 1)      # recorded, not zero
        self.assertEqual(manifest["device_reports"][0]["season"], "Autumn 2025")


class SelectionListTests(unittest.TestCase):
    """An export is a record, not something you can open as a selection."""

    def test_exports_are_listed_under_their_selection(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = _config(tmp)
        cfg.paths.t2_root = tmp / "t2out"
        _build_season(cfg.paths.output_root, "CD901", "Autumn 2025",
                      date(2025, 9, 27), date(2025, 9, 24), date(2025, 9, 24), seed=3)
        rec = t2_selections.create(cfg, {
            "participants": ["CD901"], "devices": ["actigraph", "mieye", "expiwell"],
            "initials": "YK", "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        _, sel = t2_selections.load_selection(cfg, rec["batch_id"])
        t2_selections.export_write(cfg, rec["batch_id"], sel, {"approver": "YK"})

        listed = t2_selections.list_selections(cfg)
        self.assertEqual([r["batch_id"] for r in listed], [rec["batch_id"]])   # the export is not a row
        self.assertEqual(len(listed[0]["exports"]), 1)
        self.assertEqual(listed[0]["exports"][0]["by"], "YK")


class MessyFolderNameTests(unittest.TestCase):
    """A real folder is named "Autumn  2025" with two spaces; reports still ship."""

    def test_double_spaced_folder_still_matches(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = _config(tmp)
        cfg.paths.t2_root = tmp / "t2out"
        _build_season(cfg.paths.output_root, "CD901", "Autumn  2025",
                      date(2025, 9, 27), date(2025, 9, 24), date(2025, 9, 24), seed=5)
        (cfg.paths.output_root / "CD901" / "Autumn  2025" / "Actigraph" / "CD901_report.pdf").write_bytes(b"%PDF")
        rec = t2_selections.create(cfg, {
            "participants": ["CD901"], "devices": ["actigraph", "mieye", "expiwell"],
            "initials": "YK", "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        _, sel = t2_selections.load_selection(cfg, rec["batch_id"])
        result = t2_selections.export_write(cfg, rec["batch_id"], sel, {"approver": "YK"})
        pdfs = list((Path(result["destination"]) / "reports").rglob("*.pdf"))
        self.assertEqual(len(pdfs), 1)


class ChartsPdfTests(unittest.TestCase):
    """The export ships the charts, and still works where matplotlib is absent."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = _config(cls.tmp)
        cls.cfg.paths.t2_root = cls.tmp / "t2out"
        for pid, seed in (("CD901", 21), ("CD902", 22)):
            _build_season(cls.cfg.paths.output_root, pid, "Autumn 2025",
                          date(2025, 9, 27), date(2025, 9, 24), date(2025, 9, 24), seed=seed)
        cls.rec = t2_selections.create(cls.cfg, {
            "participants": ["CD901", "CD902"], "devices": ["actigraph", "mieye", "expiwell"],
            "initials": "YK", "name": "charts test",
            "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        _, cls.sel = t2_selections.load_selection(cls.cfg, cls.rec["batch_id"])

    def test_pdf_is_written_and_recorded(self):
        from cdcompliance import t2_report
        if not t2_report.available():
            self.skipTest("matplotlib not installed")
        result = t2_selections.export_write(self.cfg, self.rec["batch_id"], self.sel,
                                            {"approver": "YK"})
        pdf = Path(result["destination"]) / "report.pdf"
        self.assertTrue(pdf.is_file())
        self.assertGreater(pdf.stat().st_size, 20_000)        # real pages, not a stub
        self.assertEqual(pdf.read_bytes()[:4], b"%PDF")
        manifest = json.loads((Path(result["destination"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(manifest["charts_pdf"]["pages"], 4)
        self.assertEqual(manifest["charts_pdf"]["timeline_pages"], 2)   # one per participant-season

    def test_export_survives_without_matplotlib(self):
        from cdcompliance import t2_report
        original = t2_report.available
        t2_report.available = lambda: False
        try:
            result = t2_selections.export_write(self.cfg, self.rec["batch_id"], self.sel,
                                                {"approver": "YK"})
            dest = Path(result["destination"])
            self.assertFalse((dest / "report.pdf").exists())
            self.assertTrue((dest / "data_long.csv").is_file())        # data still exported
            manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("skipped", str(manifest["charts_pdf"]))
            preview = t2_selections.export_preview(self.cfg, self.rec["batch_id"], self.sel, {})
            entry = next(f for f in preview["files"] if f["name"] == "report.pdf")
            self.assertIn("matplotlib is not installed", entry["what"])
        finally:
            t2_report.available = original

    def test_two_exports_in_the_same_second_get_their_own_folders(self):
        a = t2_selections.export_write(self.cfg, self.rec["batch_id"], self.sel, {"approver": "YK"})
        b = t2_selections.export_write(self.cfg, self.rec["batch_id"], self.sel, {"approver": "YK"})
        self.assertNotEqual(a["destination"], b["destination"])
        self.assertTrue(Path(b["destination"]).is_dir())
        manifest = json.loads((Path(b["destination"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["export_id"], Path(b["destination"]).name)
