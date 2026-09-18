"""T2 phase P2: pairing, lags and the within/between correlation split.

Pure data tests - no files, no study data.

    python -m unittest tests.test_t2_stats -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cdcompliance import t2_stats  # noqa: E402
from cdcompliance.t2_data import LONG_COLUMNS  # noqa: E402

START = date(2026, 1, 5)


def _rows(participant, variable, device, values, *, start=START, level="day",
          valid=True, partial=False, night_shift=0, in_window=True,
          season="Winter 2026", season_n=1, season_label="Winter 2025/26"):
    out = []
    for i, v in enumerate(values):
        d = start + timedelta(days=i)
        out.append({
            "participant": participant, "display_id": participant, "season": season,
            "season_n": season_n, "season_key": (season_label or "").lower().replace(" ", "-"),
            "season_label": season_label, "season_type": (season_label or " ").split()[0],
            "season_year": 2025, "device": device, "variable": variable, "level": level,
            "date": d, "night_of": d - timedelta(days=night_shift), "day_of_study": None,
            "value": v, "valid": valid, "partial": partial, "in_window": in_window,
            "window_day": i + 1, "recording": "rec",
        })
    return out


def _frame(rows):
    return pd.DataFrame(rows, columns=LONG_COLUMNS)


class PairingTests(unittest.TestCase):
    def setUp(self):
        # y's night_of is the previous date, so "that night" pairs y[i+1] with x[i]
        self.long = _frame(
            _rows("CD901", "light_mel_mean", "mieye", [1, 2, 3, 4, 5]) +
            _rows("CD901", "diary_tst", "expiwell", [10, 20, 30, 40, 50], night_shift=1))

    def test_same_day(self):
        p = t2_stats.pair(self.long, "light_mel_mean", "diary_tst", "same")
        self.assertEqual(len(p), 5)
        self.assertEqual(list(p["y"]), [10, 20, 30, 40, 50])

    def test_that_night(self):
        p = t2_stats.pair(self.long, "light_mel_mean", "diary_tst", "night")
        self.assertEqual(list(zip(p["x"], p["y"])), [(1, 20), (2, 30), (3, 40), (4, 50)])

    def test_next_day(self):
        p = t2_stats.pair(self.long, "light_mel_mean", "diary_tst", "next")
        self.assertEqual(list(zip(p["x"], p["y"])), [(1, 20), (2, 30), (3, 40), (4, 50)])

    def test_night_and_next_differ_for_noon_window_rows(self):
        # An Actigraph noon-to-noon row's night_of IS its date, so "that night"
        # pairs same-date rows while "next day" shifts by one.
        long = _frame(
            _rows("CD901", "light_mel_mean", "mieye", [1, 2, 3, 4, 5]) +
            _rows("CD901", "act_l5_hour", "actigraph", [10, 20, 30, 40, 50], night_shift=0))
        night = t2_stats.pair(long, "light_mel_mean", "act_l5_hour", "night")
        nxt = t2_stats.pair(long, "light_mel_mean", "act_l5_hour", "next")
        self.assertEqual(list(zip(night["x"], night["y"])), [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)])
        self.assertEqual(list(zip(nxt["x"], nxt["y"])), [(1, 20), (2, 30), (3, 40), (4, 50)])

    def test_unknown_lag(self):
        with self.assertRaises(ValueError):
            t2_stats.pair(self.long, "light_mel_mean", "diary_tst", "yesterday")

    def test_filters_drop_days(self):
        # T2 does NOT re-apply the device's valid-day rule: everything here has
        # already passed the Visualise panel, so every recorded day is kept.
        long = _frame(
            _rows("CD901", "a", "mieye", [1, 2, 3, 4, 5]) +
            _rows("CD901", "b", "expiwell", [1, 2, 3, 4, 5], valid=False))
        self.assertEqual(len(t2_stats.pair(long, "a", "b", "same")), 5)
        self.assertEqual(len(t2_stats.pair(long, "a", "b", "same", valid_only=True)), 0)
        # weeks: window days 1-7 only
        long2 = _frame(_rows("CD901", "a", "mieye", list(range(14))) +
                       _rows("CD901", "b", "expiwell", list(range(14))))
        self.assertEqual(len(t2_stats.pair(long2, "a", "b", "same", weeks=(1, 1))), 7)
        partial = _frame(_rows("CD901", "a", "mieye", [1, 2, 3], partial=True) +
                         _rows("CD901", "b", "expiwell", [1, 2, 3]))
        self.assertEqual(len(t2_stats.pair(partial, "a", "b", "same")), 0)
        self.assertEqual(len(t2_stats.pair(partial, "a", "b", "same", exclude_partial=False)), 3)


class CorrelationTests(unittest.TestCase):
    """Within-person and between-person must not be conflated.

    Constructed so that inside every person y rises perfectly with x, while
    across people the person-means fall perfectly: within r = +1, between r = -1.
    A single pooled correlation would report neither.
    """

    def setUp(self):
        rows = []
        for i, (pid, offset) in enumerate((("CD901", 100), ("CD902", 0), ("CD903", -100))):
            xs = list(range(1 + i * 10, 11 + i * 10))
            rows += _rows(pid, "light_mel_mean", "mieye", xs)
            rows += _rows(pid, "diary_tst", "expiwell", [2 * x + offset for x in xs])
        self.long = _frame(rows)
        self.pairs = t2_stats.pair(self.long, "light_mel_mean", "diary_tst", "same")

    def test_split_recovers_both_signs(self):
        s = t2_stats.correlate(self.pairs)
        self.assertEqual((s["n_pairs"], s["n_participants"]), (30, 3))
        self.assertAlmostEqual(s["within"]["r"], 1.0, places=6)
        self.assertAlmostEqual(s["between"]["r"], -1.0, places=6)
        self.assertEqual(s["within"]["n_eff"], 28)          # 30 pairs - 3 people + 1
        self.assertEqual(s["within"]["per_person_median_r"], 1.0)
        self.assertEqual(s["within"]["n_persons_used"], 3)

    def test_ci_and_p_for_a_known_moderate_r(self):
        # r = 0.5 with n = 103 -> Fisher z CI ~ [0.34, 0.63], p < 1e-6
        out = t2_stats._fisher(0.5, 103)
        self.assertEqual(out["r"], 0.5)
        self.assertAlmostEqual(out["ci"][0], 0.34, places=2)
        self.assertAlmostEqual(out["ci"][1], 0.63, places=2)
        self.assertLess(out["p"], 1e-6)
        # a perfect or degenerate r gets no interval instead of an infinite one
        self.assertIsNone(t2_stats._fisher(1.0, 50)["ci"])
        self.assertIsNone(t2_stats._fisher(None, 50)["p"])
        self.assertIsNone(t2_stats._fisher(0.5, 3)["ci"])

    def test_too_few_points_and_flat_variables(self):
        s = t2_stats.correlate(self.pairs.head(3))
        self.assertIsNone(s["within"]["r"])
        self.assertIn("fewer than", s["note"])
        flat = _frame(_rows("CD901", "a", "mieye", [5] * 10) +
                      _rows("CD901", "b", "expiwell", list(range(10))))
        s2 = t2_stats.correlate(t2_stats.pair(flat, "a", "b", "same"))
        self.assertIsNone(s2["within"]["r"])                 # zero variance, no r
        self.assertIn("one participant", s2["note"])

    def test_fdr_matches_benjamini_hochberg(self):
        self.assertEqual(t2_stats.fdr([0.01, 0.02, 0.03]), [0.03, 0.03, 0.03])
        self.assertEqual(t2_stats.fdr([0.001, 0.5]), [0.002, 0.5])
        self.assertEqual(t2_stats.fdr([None, 0.04]), [None, 0.04])
        self.assertEqual(t2_stats.fdr([]), [])
        q = t2_stats.fdr([0.9, 0.01])                        # order preserved
        self.assertEqual(q, [0.9, 0.02])

    def test_matrix_is_corrected_across_pairs(self):
        m = t2_stats.matrix(self.long, ["light_mel_mean", "diary_tst"], "same")
        self.assertEqual(len(m["cells"]), 1)
        self.assertEqual(m["cells"][0]["n_pairs"], 30)
        self.assertEqual(m["cells"][0]["r"], 1.0)
        self.assertIn("q", m["cells"][0])

    def test_by_season_keys_on_the_calendar_season(self):
        rows = t2_stats.by_season(self.long, ["light_mel_mean"])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # the CALENDAR season, not the folder label or the visit number
        self.assertEqual(r["season"], "Winter 2025/26")
        self.assertEqual((r["n_participants"], r["n_days"]), (3, 30))
        self.assertEqual(r["season_ns"], [1])
        self.assertEqual(r["mean"], 15.5)                    # mean of 5.5, 15.5, 25.5
        self.assertEqual((r["min"], r["max"]), (5.5, 25.5))


if __name__ == "__main__":
    unittest.main()


class CompareTests(unittest.TestCase):
    """P3: groups, calendar seasons and participants, compared like for like."""

    def setUp(self):
        # Two people measured in two calendar seasons. Autumn values sit 10
        # higher than winter for BOTH people, so the paired difference is exact.
        rows = []
        for pid, base in (("CD901", 100), ("CD902", 140), ("CD903", 110), ("CD904", 150)):
            rows += _rows(pid, "light_mel_mean", "mieye", [base + i for i in range(10)],
                          season="Autumn 2025", season_n=1, season_label="Autumn 2025")
            rows += _rows(pid, "light_mel_mean", "mieye", [base - 10 + i for i in range(10)],
                          start=date(2026, 1, 5), season="Winter 2026", season_n=2,
                          season_label="Winter 2025/26")
        self.long = _frame(rows)

    def test_seasons_compare_by_calendar_season(self):
        out = t2_stats.compare(self.long, "light_mel_mean", by="season")
        keys = [r["key"] for r in out["rows"]]
        self.assertEqual(keys, ["Autumn 2025", "Winter 2025/26"])
        autumn = next(r for r in out["rows"] if r["key"] == "Autumn 2025")
        self.assertEqual((autumn["n_participants"], autumn["n_days"]), (4, 40))
        self.assertEqual(autumn["mean"], 129.5)          # mean of 104.5, 144.5, 114.5, 154.5
        pair = out["pairs"][0]
        self.assertEqual(pair["paired_diff"], 10.0)      # every person is +10 in autumn
        self.assertEqual(pair["n_shared_participants"], 4)
        self.assertIn("q", pair)

    def test_groups_only_include_assigned_participants(self):
        out = t2_stats.compare(self.long, "light_mel_mean", by="group",
                               groups={"A": ["CD901", "CD903"], "B": ["CD902", "CD904"]})
        self.assertEqual([r["key"] for r in out["rows"]], ["A", "B"])
        self.assertEqual([r["n_participants"] for r in out["rows"]], [2, 2])
        self.assertAlmostEqual(out["pairs"][0]["diff"], -40.0, places=6)
        # one participant per group: a mean exists, a difference CI cannot
        thin = t2_stats.compare(self.long, "light_mel_mean", by="group",
                                groups={"A": ["CD901"], "B": ["CD902"]})
        self.assertIsNone(thin["pairs"][0]["diff"])
        # an unassigned participant is simply not compared
        one = t2_stats.compare(self.long, "light_mel_mean", by="group", groups={"A": ["CD901"]})
        self.assertEqual(len(one["rows"]), 1)
        none = t2_stats.compare(self.long, "light_mel_mean", by="group", groups={})
        self.assertEqual(none["rows"], [])
        self.assertIn("are in a group", none["note"])

    def test_one_participant_across_the_year(self):
        out = t2_stats.compare(self.long, "light_mel_mean", by="season", participants=["CD901"])
        self.assertEqual([r["n_participants"] for r in out["rows"]], [1, 1])
        self.assertEqual(out["rows"][0]["mean"], 104.5)

    def test_unknown_split_and_empty_range(self):
        with self.assertRaises(ValueError):
            t2_stats.compare(self.long, "light_mel_mean", by="astrology")
        empty = t2_stats.compare(self.long, "light_mel_mean", by="season", weeks=(9, 9))
        self.assertEqual(empty["rows"], [])


class SmallSampleTests(unittest.TestCase):
    def test_two_people_get_no_between_person_r(self):
        rows = []
        for i, pid in enumerate(("CD901", "CD902")):
            xs = [1 + i * 10 + j for j in range(8)]
            rows += _rows(pid, "light_mel_mean", "mieye", xs)
            rows += _rows(pid, "diary_tst", "expiwell", [2 * x for x in xs])
        pairs = t2_stats.pair(_frame(rows), "light_mel_mean", "diary_tst", "same")
        s = t2_stats.correlate(pairs)
        self.assertIsNone(s["between"]["r"])          # a line through 2 points is not a finding
        self.assertEqual(s["between"]["n"], 2)
        self.assertIn("at least 3", s["note"])
        self.assertIsNotNone(s["within"]["r"])        # within-person is still fine


class OverlapTests(unittest.TestCase):
    """Clock hours wrap: 23:00 and 01:00 are two hours apart, not twenty-two."""

    def test_circular_difference(self):
        self.assertEqual(t2_stats.circular_difference(1, 23), 2.0)
        self.assertEqual(t2_stats.circular_difference(23, 1), -2.0)
        self.assertEqual(t2_stats.circular_difference(3, 2), 1.0)

    def test_agreement_counts_days_within_tolerance(self):
        # L2 onset sits within an hour of L5 onset on 4 of 5 days, and one of
        # those days straddles midnight (23 vs 0).
        long = _frame(_rows("CD901", "light_l2_onset", "mieye", [23, 1, 2, 3, 10]) +
                      _rows("CD901", "act_l5_hour", "actigraph", [0, 1, 3, 3, 4]))
        pairs = t2_stats.pair(long, "light_l2_onset", "act_l5_hour", "same")
        out = t2_stats.agreement(pairs, circular=True, tolerance=1.0)
        self.assertEqual(out["n_days"], 5)
        self.assertEqual(out["within_tolerance"], 4)
        self.assertEqual(out["pct_within_tolerance"], 80.0)
        self.assertEqual(out["median_absolute_difference"], 1.0)
        self.assertEqual(out["per_participant"][0]["display_id"], "CD901")

    def test_straight_difference_when_not_circular(self):
        long = _frame(_rows("CD901", "diary_tst", "expiwell", [400, 420, 440]) +
                      _rows("CD901", "diary_tib", "expiwell", [450, 460, 470]))
        pairs = t2_stats.pair(long, "diary_tst", "diary_tib", "same")
        out = t2_stats.agreement(pairs, circular=False, tolerance=15)
        self.assertEqual(out["median_difference"], -40.0)
        self.assertEqual(out["within_tolerance"], 0)
        self.assertIsNotNone(out["limits_of_agreement"])

    def test_no_pairs_is_explained(self):
        out = t2_stats.agreement(t2_stats.pair(_frame(_rows("CD901", "a", "mieye", [1, 2])),
                                               "a", "b", "same"), circular=True)
        self.assertEqual(out["n_days"], 0)
        self.assertIn("no days", out["note"])
