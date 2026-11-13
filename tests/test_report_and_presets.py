"""F1 compliance reports and C2 topic presets.

Synthetic folders in a temp directory; nothing here reads the study drive.

    python -m unittest tests.test_report_and_presets -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import presets, report  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(self.tmp / "missing.yaml")
        self.cfg.paths.source_root = self.tmp / "staging"
        self.cfg.paths.output_root = self.tmp / "master"
        self.cfg.runs_dir = self.tmp / "runs"
        (self.cfg.paths.source_root).mkdir(parents=True, exist_ok=True)

    def recording(self, pid, season, device, stem, verdict, pct=90.0, day="2025-10-02"):
        dev = self.cfg.paths.output_root / pid / season / device
        dev.mkdir(parents=True, exist_ok=True)
        (dev / f"{stem}.bin").write_bytes(b"\0" * 32)
        (dev / f"{stem}_60s.csv").write_text("Time,SVM_sum\n", encoding="utf-8")
        (dev / f"{stem}_compliance.xlsx").write_bytes(b"x")
        (dev / f"{stem}_60s_daily_compliance.csv").write_text(
            f"date,valid_day,pct_compliance\n{day},True,{pct}\n", encoding="utf-8")
        (dev / f"{stem}_compliance.json").write_text(json.dumps(
            {"compliance": {"verdict": verdict,
                            "summary": {"valid_days": 7, "total_days": 8,
                                        "pct_compliance_mean": pct}}}), encoding="utf-8")
        src = self.cfg.paths.source_root / pid / season / device
        src.mkdir(parents=True, exist_ok=True)
        (src / f"{stem}.bin").write_bytes(b"\0" * 32)


class Report(Base):
    def setUp(self):
        super().setUp()
        self.recording("CD901", "Autumn 2025", "Actigraph",
                       "CD901_left wrist_109248_2025-10-02 10-42-59", "PASS", 95.0)
        self.recording("CD902", "Autumn 2025", "Actigraph",
                       "CD902_left wrist_109249_2025-10-05 10-42-59", "FAIL", 40.0)

    def test_it_counts_verdicts_across_the_selection(self):
        r = report.build(self.cfg, ["CD901", "CD902"])
        self.assertEqual(r["n_recordings"], 2)
        self.assertEqual(r["verdicts"]["PASS"], 1)
        self.assertEqual(r["verdicts"]["FAIL"], 1)
        self.assertEqual(r["pct_compliance_mean"], 67.5)

    def test_it_rolls_up_by_device_and_by_season(self):
        r = report.build(self.cfg, ["CD901", "CD902"])
        self.assertEqual([d["device"] for d in r["by_device"]], ["actigraph"])
        self.assertEqual(r["by_device"][0]["n"], 2)
        self.assertEqual(r["by_season"][0]["participants"], 2)

    def test_an_unprocessed_device_shows_as_outstanding(self):
        """A device with input and no output is the whole point of the report."""
        src = self.cfg.paths.source_root / "CD901" / "Autumn 2025" / "MiEYE"
        src.mkdir(parents=True, exist_ok=True)
        (src / "CD901_mieye-logged.csv").write_text("Date,melanopic\n", encoding="utf-8")
        r = report.build(self.cfg, ["CD901"])
        self.assertTrue(any(o["device"] == "mieye" for o in r["outstanding"]))

    def test_a_participant_with_no_output_folder_does_not_break_it(self):
        r = report.build(self.cfg, ["CD901", "CD999"])
        self.assertEqual(r["n_participants"], 2)
        self.assertEqual(r["n_recordings"], 1)

    def test_html_renders_the_numbers_not_a_template(self):
        page = report.render_html(report.build(self.cfg, ["CD901", "CD902"]))
        self.assertIn("<h1>", page)
        self.assertIn("CD901", page)
        self.assertNotIn("{", page.split("<style>")[0])

    def test_saving_writes_html_json_and_a_workbook_to_the_private_side(self):
        saved = report.save(self.cfg, report.build(self.cfg, ["CD901"]))
        for key in ("html", "json", "xlsx"):
            self.assertTrue(Path(saved[key]).exists(), key)
        # reports name participants, so they must never land beside the public
        # stripped batch records
        self.assertIn("_private", saved["dir"])

    def test_a_batch_id_is_carried_into_the_title(self):
        r = report.build(self.cfg, ["CD901"], batch_id="PRE-20261109-120000-YK")
        self.assertIn("PRE-20261109-120000-YK", r["title"])


class Presets(Base):
    def test_every_shipped_preset_resolves_against_the_dictionary(self):
        for p in presets.load():
            self.assertEqual(p["unknown"], [], f"{p['id']} names variables that do not exist")
            self.assertEqual(p["unknown_devices"], [], p["id"])
            self.assertTrue(p["n_available"] > 0, p["id"])

    def test_a_preset_never_carries_participants_or_seasons(self):
        raw = presets._load_raw()
        for p in raw["presets"]:
            self.assertNotIn("participants", p, p.get("id"))
            self.assertNotIn("seasons", p, p.get("id"))

    def test_an_empty_variable_list_means_every_variable_for_those_devices(self):
        p = presets.get("everything-available")
        self.assertTrue(p["all_variables"])
        self.assertGreater(p["n_available"], 30)

    def test_each_preset_says_what_it_cannot_supply_yet(self):
        p = presets.get("seasonal-light-and-shift-work")
        self.assertTrue(any("mEDI" in m or "EDI" in m for m in p["missing"]))

    def test_an_unknown_variable_is_reported_not_dropped_silently(self):
        path = self.tmp / "p.yaml"
        path.write_text("version: 1\npresets:\n  - id: t\n    name: T\n"
                        "    devices: [actigraph]\n    variables: [act_is, act_nope]\n",
                        encoding="utf-8")
        p = presets.load(path)[0]
        self.assertEqual(p["variables"], ["act_is"])
        self.assertEqual(p["unknown"], ["act_nope"])

    def test_a_missing_presets_file_is_not_an_error(self):
        self.assertEqual(presets.load(self.tmp / "nothing.yaml"), [])


if __name__ == "__main__":
    unittest.main()


class ReadyDecision(Base):
    """The report's own answer to "can I run this", which the grid colour encodes."""

    def test_a_folder_missing_a_device_is_not_ready_and_says_why(self):
        self.recording("CD901", "Autumn 2025", "Actigraph",
                       "CD901_left wrist_109248_2025-10-02 10-42-59", "PASS")
        r = report.build(self.cfg, ["CD901"])
        person = r["people"][0]
        self.assertFalse(person["ready"])
        self.assertIn("data incomplete", person["why_not_ready"])
        self.assertTrue(person["devices_missing"])

    def test_not_ready_folders_are_counted_separately(self):
        self.recording("CD901", "Autumn 2025", "Actigraph",
                       "CD901_left wrist_109248_2025-10-02 10-42-59", "PASS")
        r = report.build(self.cfg, ["CD901"])
        self.assertEqual(r["n_ready"], 0)
        self.assertEqual(r["n_data_complete"], 0)

    def test_a_selected_folder_with_no_staging_still_appears(self):
        r = report.build(self.cfg, ["CD999"])
        self.assertEqual(len(r["people"]), 1)
        self.assertFalse(r["people"][0]["ready"])
        self.assertIn("no staging folder", r["people"][0]["why_not_ready"][0])

    def test_the_workbook_colours_the_ready_column(self):
        from openpyxl import load_workbook
        self.recording("CD901", "Autumn 2025", "Actigraph",
                       "CD901_left wrist_109248_2025-10-02 10-42-59", "PASS")
        out = self.tmp / "r.xlsx"
        self.assertIsNotNone(report.write_xlsx(report.build(self.cfg, ["CD901"]), out))
        wb = load_workbook(out)
        self.assertIn("Key", wb.sheetnames)
        self.assertIn("Participants", wb.sheetnames)
        ws = wb["Participants"]
        # row 3 is the header (row 1 is the note), so row 4 is the first folder
        cell = ws.cell(row=4, column=2)
        self.assertEqual(cell.value, "not ready")
        self.assertEqual(cell.fill.fgColor.rgb[-6:], report.FILLS["bad"][0])
