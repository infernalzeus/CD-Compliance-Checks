"""T2 phase P0: pseudonymous IDs, batch ids and the T2 data layer.

Runs on a synthetic output tree in a temporary folder - never on study data.

    python -m unittest tests.test_t2_p0 -v
"""
from __future__ import annotations

import csv
import json
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import batches, pseudo_id, t2_data  # noqa: E402
from cdcompliance.config import load_config  # noqa: E402


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _config(tmp: Path):
    cfg = load_config(tmp / "missing-config.yaml")
    cfg.paths.source_root = tmp / "staging"
    cfg.paths.output_root = tmp / "master"
    cfg.runs_dir = tmp / "runs"
    return cfg


def _build_season(out: Path, pid: str, season: str, act_start: date, light_start: date,
                  esm_start: date, esm_dates_gap: bool = True, affect_dated: bool = True,
                  seed: int | None = None) -> None:
    """Write a synthetic participant-season.

    With a seed the values vary day to day, and sleep is built to depend on the
    previous evening's light, so the cross-device views have something real to
    find (used by the sandbox dashboard). Without a seed every value is
    constant, which is what the assertions below rely on.
    """
    rnd = random.Random(seed) if seed is not None else None

    def jitter(base: float, spread: float, digits: int = 1) -> float:
        return base if rnd is None else round(base + rnd.uniform(-spread, spread), digits)

    s = out / pid / season
    stem = f"{pid}_left wrist_100000_{act_start + timedelta(days=21)} 09-00-00"
    # Actigraph: 22 dates, first and last hold 720 epochs (noon start / noon end)
    act_dates = [act_start + timedelta(days=i) for i in range(22)]
    (s / "Actigraph").mkdir(parents=True, exist_ok=True)
    (s / "Actigraph" / f"{stem}_60s.csv").write_text("Time,SVM_sum\n", encoding="utf-8")
    wear = {d: jitter(20, 4) for d in act_dates}
    _write_csv(s / "Actigraph" / f"{stem}_60s_daily_compliance.csv", [
        {"date": d, "epochs": 720 if i in (0, 21) else 1440, "wear_hours": wear[d],
         "nonwear_hours": round(24 - wear[d], 1),
         "pct_compliance": min(100, round(wear[d] / 16 * 100)),
         "hourly_svm_range": jitter(50, 20), "within_day_svm_sd": 5, "svm_mean": jitter(60, 25),
         "valid_day": i != 5, "invalid_reason": ""}
        for i, d in enumerate(act_dates)])
    _write_csv(s / "Actigraph" / f"{stem}_60s_daily.csv", [
        {"fname": f"{stem}_60s.csv", "date": d, "M10_hour": jitter(14, 2, 0),
         "M10_mean_activity": jitter(100, 30), "M10_actual_activity": jitter(110, 30),
         "L5_hour": jitter(3, 1, 0), "L5_mean_activity": jitter(20, 8),
         "L5_actual_activity": jitter(18, 8)}
        for d in act_dates])
    _write_csv(s / "Actigraph" / f"{stem}_60s_nonparametric.csv", [
        {"fname": "x", "processing_timestamp": "t", "IS": 0.5, "IV": 0.8, "M10_hour": 14,
         "M10_mean_activity": 1, "L5_hour": 3, "L5_mean_activity": 1}])
    # MiEYE: 30 calendar days, first is partial
    light = f"M00 {pid} S1-{light_start} 09-00-00-logged"
    ldates = [light_start + timedelta(days=i) for i in range(30)]
    mel = {d: jitter(50, 35) for d in ldates}
    _write_csv(s / "MiEYE" / f"{light}_luminosity_metrics.csv", [
        {"date": d, "n_epochs": 900 if i == 0 else 1440, "melanopic_mean": mel[d],
         "melanopic_median": jitter(5, 3), "melanopic_max": jitter(900, 300, 0),
         "day_mean": jitter(80, 40), "night_mean": jitter(0.2, 0.15, 2),
         "tat_min": jitter(30, 25, 0), "tbt_min": jitter(400, 120, 0),
         "log_mean": jitter(0.5, 0.4, 2)}
        for i, d in enumerate(ldates)])
    _write_csv(s / "MiEYE" / f"{light}_daily_compliance.csv", [
        {"date": d, "epochs": 900 if i == 0 else 1440, "light_hours": jitter(12, 5),
         "day_mean": jitter(80, 40), "night_mean": jitter(0.2, 0.15, 2), "day_night_ratio": 400,
         "pct_compliance": 100, "melanopic_mean": mel[d], "valid_day": True, "invalid_reason": "",
         "tat_min": jitter(30, 25, 0), "tbt_min": jitter(400, 120, 0),
         "log_mean": jitter(0.5, 0.4, 2)}
        for i, d in enumerate(ldates)])
    _write_csv(s / "MiEYE" / f"{light}_light_windows.csv", [
        {"date": d, "hours_with_data": 24, "M5_onset": jitter(10, 2, 0),
         "M5_mean": jitter(300, 120, 0), "M7_onset": jitter(9, 2, 0), "M7_mean": jitter(250, 100, 0),
         "L2_onset": 23 if i % 2 else 1, "L2_mean": jitter(0.1, 0.08, 2)}
        for i, d in enumerate(ldates)])
    # Expiwell: 15 days of study spread over 19 calendar days (skips)
    offsets = [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, 16, 17, 18] if esm_dates_gap else list(range(15))
    ex = s / "Expiwell"
    # A diary row dated D reports the night that started on D-1, so it is built
    # from that evening's light: brighter -> shorter sleep, plus a person offset.
    person = 0 if rnd is None else rnd.uniform(-25, 25)

    def diary(d: date) -> dict:
        if rnd is None:
            return {"SOL_min": 10, "WASO_min": 10, "TST_min": 490, "TIB_min": 570, "SE_pct": 86}
        lux = mel.get(d - timedelta(days=1), 50)
        tst = round(500 - 0.6 * lux + person + rnd.uniform(-20, 20))
        sol = round(max(2, 8 + 0.08 * lux + rnd.uniform(-5, 5)))
        waso = round(max(0, 10 + rnd.uniform(-6, 12)))
        tib = tst + sol + waso + round(rnd.uniform(5, 30))
        return {"SOL_min": sol, "WASO_min": waso, "TST_min": tst, "TIB_min": tib,
                "SE_pct": round(tst / tib * 100, 1)}

    sleep_rows = []
    for i, o in enumerate(offsets):
        d = esm_start + timedelta(days=o)
        m = diary(d)
        sleep_rows.append({
            "participant": pid, "survey": "Sleep-Diary", "day": i + 1, "date": d,
            "into_bed": "22:00", "lights_out": "22:30", "final_wake": "07:00",
            "out_of_bed": "07:30", "SOL_min": m["SOL_min"],
            "n_awakenings": 1 if rnd is None else rnd.randint(0, 4), "WASO_min": m["WASO_min"],
            "TIB_min": m["TIB_min"], "TST_min": m["TST_min"],
            "TST_hours": round(m["TST_min"] / 60, 2), "SE_pct": m["SE_pct"],
            "quality": 3 if rnd is None else rnd.randint(1, 5),
            "restedness": 3 if rnd is None else rnd.randint(1, 5)})
    _write_csv(ex / f"{pid}_expiwell_sleep_metrics.csv", sleep_rows)
    affect, series = [], []
    for i, o in enumerate(offsets):
        for occ, hour in ((1, 11), (2, 17)):
            ts = datetime.combine(esm_start + timedelta(days=o), datetime.min.time()) + timedelta(hours=hour)
            row = {"participant": pid, "survey": "Affect", "day": i + 1, "occasion": occ}
            if affect_dated:
                row.update({"timestamp": ts.isoformat(sep=" "), "date": ts.date().isoformat()})
            row.update({"positive_affect": 4 + occ if rnd is None else jitter(4.2, 1.6, 2),
                        "negative_affect": 2 if rnd is None else jitter(2.4, 1.2, 2),
                        "n_positive_items": 6, "n_negative_items": 7})
            affect.append(row)
            series.append({"participant": pid, "survey": "Affect", "day": i + 1, "occasion": occ,
                           "timestamp": ts.isoformat(sep=" "), "item": "Relaxed", "answer": "x", "value": 3})
    _write_csv(ex / f"{pid}_expiwell_affect.csv", affect)
    _write_csv(ex / f"{pid}_expiwell_item_series.csv", series)
    _write_csv(ex / f"{pid}_expiwell_metrics.csv", [
        {"survey": "Affect", "type": "signal", "scored": True, "expected": 30, "completed": 26, "missed": 4,
         "response_rate_pct": 86.7, "days_expected": 15, "days_with_response": 15, "complete_days": 11,
         "median_latency_min": 9, "pct_within_window": 100, "median_duration_sec": 39,
         "detected_type": "signal", "n_distinct_windows": 25}])


class PseudoIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = _config(self.tmp)

    def test_off_by_default_keeps_real_ids(self):
        self.assertFalse(self.cfg.privacy.pseudonymise)
        self.assertEqual(pseudo_id.display_id(self.cfg, "CD052"), "CD052")
        self.assertEqual(pseudo_id.mapping(self.cfg, ["CD052", "CD052"]), {"CD052": "CD052"})

    def test_on_requires_a_key(self):
        self.cfg.privacy.pseudonymise = True
        with self.assertRaises(pseudo_id.PseudoIdError):
            pseudo_id.display_id(self.cfg, "CD052")
        self.assertFalse(pseudo_id.status(self.cfg)["ok"])

    def test_keyed_deterministic_and_not_id_like(self):
        key_path = pseudo_id.generate_key(self.tmp / "keys" / "pid.key")
        with self.assertRaises(pseudo_id.PseudoIdError):
            pseudo_id.generate_key(key_path)                      # never overwrites
        self.cfg.privacy.pseudonymise = True
        self.cfg.privacy.key_file = str(key_path)
        ids = [f"CD{n:03d}" for n in range(1000)]
        pids = pseudo_id.mapping(self.cfg, ids)
        self.assertEqual(len(set(pids.values())), 1000)
        self.assertEqual(pseudo_id.display_id(self.cfg, " cd052 "), pids["CD052"])
        for pid in pids.values():
            self.assertRegex(pid, r"^P-[0-9abefghjkmnpqrstvwxyz]{10}$")
            batches.assert_public_safe({"ids": pid})              # passes the leak guard
        other = pseudo_id.make_pid("CD052", b"x" * 32)
        self.assertNotEqual(other, pids["CD052"])                 # different key, different PID
        self.assertNotIn(key_path.read_text().strip(), json.dumps(pseudo_id.status(self.cfg)))


class BatchIdTests(unittest.TestCase):
    def test_pre_has_no_season_and_t2_is_seasonal(self):
        when = datetime(2026, 9, 17, 14, 32, 0)
        self.assertEqual(batches.make_batch_id("PRE", "YK", when), "PRE-20260917-143200-YK")
        self.assertEqual(batches.make_batch_id("T2", "YK", when, [4, 2]), "T2-20260917-143200-YK-s24")
        self.assertEqual(batches.season_tag({3, 1, 2}), "s123")
        with self.assertRaises(ValueError):
            batches.make_batch_id("T2", "YK", when)

    def test_t2_recorder_writes_seasons_and_archives(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = _config(tmp)
        rec = batches.BatchRecorder(cfg, stage="T2", mode="new", initials="yk",
                                    participants=["CD001"], seasons=[2, 4])
        public = rec.finish("done")
        self.assertTrue(public["batch_id"].endswith("-YK-s24"))
        self.assertEqual(public["seasons"], [2, 4])
        batches.archive_batch(cfg, public["batch_id"], "YK")
        self.assertTrue((batches.public_dir(cfg) / "_archive" / f"{public['batch_id']}.json").is_file())


class T2DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = _config(cls.tmp)
        out = cls.cfg.paths.output_root
        # Folder labels deliberately misleading: "Winter 2026" holds the EARLIER data.
        _build_season(out, "CD901", "Winter 2026", date(2026, 1, 7), date(2026, 1, 4), date(2026, 1, 4))
        _build_season(out, "CD901", "Autumn  2026", date(2026, 10, 7), date(2026, 10, 4), date(2026, 10, 4),
                      affect_dated=False)
        # Staging has a later season that isn't processed yet (dated filename only).
        (cls.cfg.paths.source_root / "CD901" / "Spring 2027" / "Actigraph").mkdir(parents=True)
        (cls.cfg.paths.source_root / "CD901" / "Spring 2027" / "Actigraph" /
         "CD901_left wrist_100000_2027-04-20 09-00-00.bin").write_bytes(b"")
        cls.frame = t2_data.load(cls.cfg, t2_data.T2Selection(
            participants=["CD901"], devices=["actigraph", "mieye", "expiwell"]))

    def _rows(self, variable, season_n=1):
        L = self.frame.long
        return L[(L["variable"] == variable) & (L["season_n"] == season_n)].sort_values("date")

    def test_dictionary_is_consistent(self):
        ids = [v["id"] for v in t2_data.variables()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(t2_data.unlisted_columns(self.cfg, ["CD901"]), {})

    def test_seasons_numbered_by_data_not_label(self):
        numbers = t2_data.season_numbers(self.cfg, "CD901")
        self.assertEqual(numbers["Winter 2026"]["n"], 1)
        self.assertEqual(numbers["Autumn 2026"]["n"], 2)            # double space normalised
        self.assertEqual((numbers["Spring 2027"]["n"], numbers["Spring 2027"]["basis"]), (3, "filenames"))

    def test_expiwell_uses_real_dates_and_night_key(self):
        tst = self._rows("diary_tst")
        self.assertEqual(len(tst), 15)
        self.assertEqual((tst["date"].max() - tst["date"].min()).days + 1, 19)   # 15 days over 19 dates
        first = tst.iloc[0]
        self.assertEqual(first["night_of"], first["date"] - timedelta(days=1))
        self.assertEqual(first["day_of_study"], 1)

    def test_affect_date_fallback_and_daily_mean(self):
        a1 = self._rows("affect_positive", 1)
        a2 = self._rows("affect_positive", 2)
        self.assertEqual(len(a1), 15)
        self.assertEqual(len(a2), 15)                               # undated file recovered
        self.assertAlmostEqual(a1.iloc[0]["value"], 5.5)            # mean of 2 occasions
        flags = {f["flag"] for f in self.frame.flags if f.get("season_n") == 2}
        self.assertIn("affect-date-from-series", flags)

    def test_partial_days_and_noon_windows(self):
        l5 = self._rows("act_l5_hour")
        self.assertFalse(l5.iloc[0]["partial"])     # noon of day 1 -> noon of day 2 is complete
        self.assertTrue(l5.iloc[-1]["partial"])     # last date has no afternoon
        self.assertEqual(l5.iloc[0]["night_of"], l5.iloc[0]["date"])
        wear = self._rows("act_wear_hours")
        self.assertTrue(wear.iloc[0]["partial"] and wear.iloc[-1]["partial"])
        self.assertFalse(bool(wear.iloc[5]["valid"]))
        l2 = self._rows("light_l2_onset")
        for _, r in l2.head(4).iterrows():
            expected = r["date"] if r["value"] >= 12 else r["date"] - timedelta(days=1)
            self.assertEqual(r["night_of"], expected)

    def test_common_window(self):
        w = self.frame.windows.set_index("season_n").loc[1]
        # actigraph full days start Jan 8, MiEYE Jan 5, Expiwell Jan 4 -> overlap from Jan 8;
        # Expiwell ends Jan 22 -> window Jan 8 .. Jan 22
        self.assertEqual((w["start"], w["end"], w["days"]), (date(2026, 1, 8), date(2026, 1, 22), 15))
        inside = self._rows("light_mel_mean")
        self.assertEqual(int(inside["in_window"].sum()), 15)
        self.assertEqual(inside[inside["in_window"]]["window_day"].min(), 1)
        wide = t2_data.wide_day(self.frame)
        self.assertEqual(len(wide[wide["season_n"] == 1]), 15)

    def test_availability_flags(self):
        flags = {(f.get("season_n"), f["flag"]) for f in self.frame.flags}
        self.assertIn((3, "season-not-in-master"), flags)
        sel = t2_data.T2Selection(participants=["CD901"], devices=["actigraph"], seasons=[1, 4])
        frame = t2_data.load(self.cfg, sel)
        self.assertIn((4, "season-number-absent"), {(f.get("season_n"), f["flag"]) for f in frame.flags})
        (self.cfg.paths.output_root / "CD901" / "Winter 2026" / "Actigraph" /
         next(p.name for p in (self.cfg.paths.output_root / "CD901" / "Winter 2026" / "Actigraph").glob("*_60s.csv"))
         ).rename(self.tmp / "moved_60s.csv")
        frame = t2_data.load(self.cfg, t2_data.T2Selection(participants=["CD901"], devices=["actigraph"], seasons=[1]))
        self.assertIn("actigraph-60s-missing", {f["flag"] for f in frame.flags})

    def test_pseudonymised_frame_hides_real_ids(self):
        cfg = _config(self.tmp)
        cfg.privacy.pseudonymise = True
        key = self.tmp / "pid.key"
        if not key.exists():
            pseudo_id.generate_key(key)
        cfg.privacy.key_file = str(key)
        frame = t2_data.load(cfg, t2_data.T2Selection(participants=["CD901"], devices=["mieye"], seasons=[1]))
        self.assertTrue(frame.long["display_id"].str.startswith("P-").all())
        self.assertTrue(all(f["display_id"].startswith("P-") for f in frame.flags))


if __name__ == "__main__":
    unittest.main()


class T2SelectionTests(unittest.TestCase):
    """P1: sending a selection records it; the preview reads, never writes."""

    @classmethod
    def setUpClass(cls):
        from cdcompliance import t2_selections
        cls.ts = t2_selections
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = _config(cls.tmp)
        out = cls.cfg.paths.output_root
        _build_season(out, "CD901", "Winter 2026", date(2026, 1, 7), date(2026, 1, 4), date(2026, 1, 4))
        _build_season(out, "CD902", "Spring 2026", date(2026, 4, 7), date(2026, 4, 4), date(2026, 4, 4))
        _build_season(out, "CD902", "Summer 2026", date(2026, 7, 7), date(2026, 7, 4), date(2026, 7, 4))

    def _snapshot(self):
        return sorted((str(p), p.stat().st_mtime_ns) for p in self.cfg.paths.output_root.rglob("*"))

    def test_send_requires_initials_and_checks(self):
        base = {"participants": ["CD901"], "devices": ["actigraph", "mieye"]}
        with self.assertRaises(self.ts.SelectionError):
            self.ts.create(self.cfg, base)
        with self.assertRaises(self.ts.SelectionError):
            self.ts.create(self.cfg, {**base, "initials": "YK", "checks": {"verdicts_reviewed": True}})
        # Expiwell-only selections don't need the Actigraph 60 s check
        self.assertEqual(self.ts.required_checks(["expiwell"]), ["verdicts_reviewed"])

    def test_send_records_public_safe_and_private_detail(self):
        before = self._snapshot()
        public = self.ts.create(self.cfg, {
            "participants": ["CD901", "CD902"], "devices": ["actigraph", "mieye", "expiwell"],
            "initials": "yk", "name": "winter pilot",
            "checks": {"actigraph_60s": True, "verdicts_reviewed": True}})
        self.assertEqual(self._snapshot(), before)                  # master untouched
        self.assertRegex(public["batch_id"], r"^T2-\d{8}-\d{6}-YK-s12$")
        self.assertEqual(public["n_participants"], 2)
        self.assertEqual(public["n_participant_seasons"], 3)
        text = json.dumps(public)
        for leak in ("CD901", "CD902", "2026-01", "winter pilot"):
            self.assertNotIn(leak, text)
        listed = self.ts.list_selections(self.cfg)
        self.assertEqual(listed[0]["name"], "winter pilot")
        record, sel = self.ts.load_selection(self.cfg, public["batch_id"])
        self.assertEqual(sel.participants, ["CD901", "CD902"])
        self.assertEqual(sel.seasons, [1, 2])

        tl = self.ts.timeline(self.cfg, sel, "CD902", 2)
        self.assertEqual(tl["window"]["days"], 15)
        tst = next(s for s in tl["series"] if s["variable"] == "diary_tst")
        self.assertEqual(len(tst["points"]), 15)
        self.assertTrue(any(v["variable"] == "act_is" for v in tl["season_values"]))
        with self.assertRaises(self.ts.SelectionError):
            self.ts.timeline(self.cfg, sel, "CD999", 1)
        self.assertEqual(self._snapshot(), before)
