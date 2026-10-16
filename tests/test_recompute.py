"""Recompute: a Step 1 variant beside a recording, never a replacement.

Synthetic folders in a temp directory - never the study drive, and Step 1
itself is stubbed, so nothing here runs a tool or touches real data.

    python -m unittest tests.test_recompute -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import tools, variants  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402
from cdcompliance.events import EventBus  # noqa: E402

STEM = "CD901_left wrist_109414_2026-07-27 14-03-29"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(self.tmp / "missing.yaml")
        self.cfg.paths.source_root = self.tmp / "staging"
        self.cfg.paths.output_root = self.tmp / "master"
        self.dev = self.cfg.paths.output_root / "CD901" / "Autumn 2025" / "Actigraph"
        self.dev.mkdir(parents=True)
        # a processed baseline recording, with its raw .bin replicated in (copy_bin)
        (self.dev / f"{STEM}_60s.csv").write_text("Time,SVM_sum\n", encoding="utf-8")
        (self.dev / f"{STEM}.bin").write_bytes(b"x" * 2048)
        (self.dev / f"{STEM}_compliance.json").write_text(
            json.dumps({"compliance": {"verdict": "PASS"}}), encoding="utf-8")
        self.bus = EventBus()

    def stub_step1(self, *, fail=False):
        """Replace the Step 1 subprocess with a file write, capturing its options."""
        seen = {}

        def fake(config, bin_path, output_dir, bus, options=None, name="step1:process"):
            seen["options"] = dict(options or {})
            seen["bin"] = Path(bin_path)
            seen["out"] = Path(output_dir)
            if fail:
                raise tools.ToolError("step 1 blew up")
            label = tools.epoch_label((options or {}).get("epoch", "60s"))
            produced = Path(output_dir) / f"{Path(bin_path).stem}_{label}.csv"
            produced.write_text("Time,SVM_sum\n", encoding="utf-8")
            return produced

        self._real = tools.run_step1_process
        tools.run_step1_process = fake
        variants.tools.run_step1_process = fake
        self.addCleanup(self._restore)
        return seen

    def _restore(self):
        tools.run_step1_process = self._real
        variants.tools.run_step1_process = self._real


class OptionValidation(unittest.TestCase):
    def test_defaults_are_the_pipeline_baseline(self):
        d = tools.step1_defaults()
        self.assertEqual(d["epoch"], "60s")
        self.assertEqual(d["filter"], "no")
        self.assertEqual(d["summary_mode"], "full-summary")

    def test_unknown_option_is_refused(self):
        with self.assertRaises(tools.ToolError):
            tools.normalise_step1_options({"nope": "1"})

    def test_value_outside_choices_is_refused(self):
        with self.assertRaises(tools.ToolError):
            tools.normalise_step1_options({"epoch": "47s"})
        with self.assertRaises(tools.ToolError):
            tools.normalise_step1_options({"summary_mode": "rm -rf"})

    def test_non_numeric_bandpass_is_refused(self):
        with self.assertRaises(tools.ToolError):
            tools.normalise_step1_options({"low": "abc"})

    def test_epoch_label_matches_step1_naming(self):
        self.assertEqual(tools.epoch_label(" 30 s "), "30s")

    def test_changed_reports_only_differences(self):
        changed = variants.changed_from_baseline(
            tools.normalise_step1_options({"epoch": "30s"}))
        self.assertEqual(changed, {"epoch": {"baseline": "60s", "variant": "30s"}})


class Recordings(Base):
    def test_lists_the_baseline_recording_as_recomputable(self):
        rows = variants.recordings(self.cfg, "CD901", "Autumn 2025")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stem"], STEM)
        self.assertEqual(rows[0]["verdict"], "PASS")
        self.assertTrue(rows[0]["raw_present"])
        self.assertIsNone(rows[0]["blocked"])

    def test_missing_raw_bin_blocks_the_recording(self):
        (self.dev / f"{STEM}.bin").unlink()
        row = variants.recordings(self.cfg, "CD901", "Autumn 2025")[0]
        self.assertFalse(row["raw_present"])
        self.assertIn("force reprocess", row["blocked"])


class OddSeasonFolderNames(Base):
    """A season folder named freely on disk must still be found.

    Season folders are not normalised on disk ("Autumn  2025" has a double
    space) while `t2_data.season_numbers` hands back a whitespace-normalised
    name. Joining the normalised name literally produced a path that did not
    exist, so the participant silently vanished from the recompute list while
    a participant whose name happened to match exactly still showed up.
    """

    def setUp(self):
        super().setUp()
        odd = self.cfg.paths.output_root / "CD902" / "Autumn  2025" / "Actigraph"
        odd.mkdir(parents=True)
        self.odd_stem = "CD902_left wrist_109248_2025-12-02 10-42-59"
        (odd / f"{self.odd_stem}_60s.csv").write_text("Time,SVM_sum\n", encoding="utf-8")
        (odd / f"{self.odd_stem}.bin").write_bytes(b"x" * 1024)

    def test_double_spaced_season_is_found_by_its_normalised_name(self):
        rows = variants.recordings(self.cfg, "CD902", "Autumn 2025")   # single space
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stem"], self.odd_stem)
        self.assertTrue(rows[0]["raw_present"])

    def test_variant_lands_in_the_real_folder_not_a_new_one(self):
        self.stub_step1()
        rec = variants.create(self.cfg, "CD902", "Autumn 2025", self.odd_stem,
                              {"epoch": "30s"}, "YK", self.bus)
        self.assertIn("Autumn  2025", rec["folder"])          # the real, double-spaced folder
        seasons = sorted(p.name for p in (self.cfg.paths.output_root / "CD902").iterdir())
        self.assertEqual(seasons, ["Autumn  2025"])           # no stray single-space twin

    def test_a_season_that_really_is_absent_is_empty_not_an_error(self):
        self.assertEqual(variants.recordings(self.cfg, "CD902", "Winter 2031"), [])


class Create(Base):
    def test_writes_a_variant_without_touching_the_baseline(self):
        seen = self.stub_step1()
        before = sorted(p.name for p in self.dev.iterdir())
        rec = variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                              {"epoch": "30s"}, "YK", self.bus)

        # Step 1 got the epoch, and read the LOCAL .bin (never OneDrive).
        self.assertEqual(seen["options"]["epoch"], "30s")
        self.assertEqual(seen["bin"], self.dev / f"{STEM}.bin")

        folder = Path(rec["folder"])
        self.assertTrue(folder.is_dir())
        self.assertEqual(folder.parent.name, variants.VARIANTS_DIRNAME)
        self.assertTrue((folder / f"{STEM}_30s.csv").is_file())
        self.assertTrue((folder / "variant.json").is_file())

        # the baseline folder gained exactly one entry: _variants/
        after = sorted(p.name for p in self.dev.iterdir())
        self.assertEqual(set(after) - set(before), {variants.VARIANTS_DIRNAME})
        self.assertTrue((self.dev / f"{STEM}_60s.csv").is_file())

    def test_record_says_no_step2_and_no_verdict(self):
        self.stub_step1()
        rec = variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                              {"epoch": "30s"}, "YK", self.bus)
        self.assertIn("not run", rec["step2"])
        self.assertIn("not evaluated", rec["compliance"])
        self.assertTrue(rec["experimental"])
        self.assertEqual(rec["changed"], {"epoch": {"baseline": "60s", "variant": "30s"}})
        self.assertTrue(any("Cole-Kripke" in w for w in rec["warnings"]))

    def test_no_compliance_or_step2_files_are_produced(self):
        self.stub_step1()
        rec = variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                              {"epoch": "30s"}, "YK", self.bus)
        names = {p.name for p in Path(rec["folder"]).iterdir()}
        self.assertEqual(names, {"variant.json", f"{STEM}_30s.csv"})
        for bad in ("_compliance.json", "_nonparametric.csv", "_sri.csv", "_daily_compliance.csv"):
            self.assertFalse([p for p in Path(rec["folder"]).glob(f"*{bad}")], bad)

    def test_baseline_settings_are_refused(self):
        self.stub_step1()
        with self.assertRaises(variants.VariantError):
            variants.create(self.cfg, "CD901", "Autumn 2025", STEM, {}, "YK", self.bus)

    def test_missing_raw_bin_refuses_before_running_anything(self):
        seen = self.stub_step1()
        (self.dev / f"{STEM}.bin").unlink()
        with self.assertRaises(variants.VariantError) as ctx:
            variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                            {"epoch": "30s"}, "YK", self.bus)
        self.assertIn("never downloads", str(ctx.exception))
        self.assertNotIn("options", seen)        # Step 1 was never invoked

    def test_a_failed_step1_leaves_no_folder_behind(self):
        self.stub_step1(fail=True)
        with self.assertRaises(tools.ToolError):
            variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                            {"epoch": "30s"}, "YK", self.bus)
        root = variants.variants_dir(self.cfg, "CD901", "Autumn 2025", "actigraph")
        # not even an empty _variants/ parent: a failed recompute adds nothing
        self.assertFalse(root.exists())

    def test_unsupported_device_is_refused(self):
        with self.assertRaises(variants.VariantError):
            variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                            {"epoch": "30s"}, "YK", self.bus, device="mieye")

    def test_variant_shows_up_against_its_recording(self):
        self.stub_step1()
        variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                        {"epoch": "30s"}, "YK", self.bus)
        row = variants.recordings(self.cfg, "CD901", "Autumn 2025")[0]
        self.assertEqual(len(row["variants"]), 1)
        self.assertEqual(row["variants"][0]["changed"]["epoch"]["variant"], "30s")


class Discard(Base):
    def test_moves_to_discarded_and_records_who(self):
        self.stub_step1()
        rec = variants.create(self.cfg, "CD901", "Autumn 2025", STEM,
                              {"epoch": "30s"}, "YK", self.bus)
        out = variants.discard(self.cfg, "CD901", "Autumn 2025", "actigraph",
                               rec["variant_id"], "YK", reason="not needed")
        self.assertEqual(out["discarded"]["by"], "YK")
        self.assertFalse(Path(rec["folder"]).exists())
        moved = (variants.variants_dir(self.cfg, "CD901", "Autumn 2025", "actigraph")
                 / variants.DISCARDED_DIRNAME / rec["variant_id"])
        self.assertTrue(moved.is_dir())
        # gone from the listing, baseline untouched
        self.assertEqual(variants.recordings(self.cfg, "CD901", "Autumn 2025")[0]["variants"], [])
        self.assertTrue((self.dev / f"{STEM}_60s.csv").is_file())

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(variants.VariantError):
            variants.discard(self.cfg, "CD901", "Autumn 2025", "actigraph",
                             "nope", "YK")

    def test_path_traversal_in_the_id_is_refused(self):
        with self.assertRaises(variants.VariantError):
            variants.discard(self.cfg, "CD901", "Autumn 2025", "actigraph",
                             "../../../etc", "YK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
