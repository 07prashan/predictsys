"""The filter-slip engine: which selection represents a match, how the 04:00 Kathmandu-time
day cycles split the fixtures up, and that a finished slip always lands inside its odds window.
Offline and deterministic - no DB, no network, no clock.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import slips  # noqa: E402

# The Oct 9 betting day starts 4am Oct 9 Kathmandu = 8 Oct 22:15 UTC; NOW is inside it
# (11:45 Kathmandu).
NOW = dt.datetime(2026, 10, 9, 6, 0, tzinfo=dt.timezone.utc)


def day(hours_from_start: float) -> str:
    moment = slips.day_start(NOW) + dt.timedelta(hours=hours_from_start)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def football_row(i=0, kickoff_hours=12, **overrides) -> dict:
    row = {
        "league": "T1", "sport": "football", "competition": "Super Lig",
        "match_date": day(kickoff_hours)[:10], "kickoff_utc": day(kickoff_hours),
        "home_team": f"Home {i}", "away_team": f"Away {i}",
        "prob_home": 0.75, "prob_draw": 0.16, "prob_away": 0.09,
        "btts_yes_prob": 0.53, "over_1_5_prob": 0.87, "over_2_5_prob": 0.69, "over_3_5_prob": 0.47,
        "double_chance_1x_prob": 0.91, "double_chance_12_prob": 0.84, "double_chance_x2_prob": 0.25,
    }
    row.update(overrides)
    return row


class BestLegTest(unittest.TestCase):
    def test_it_picks_the_most_likely_selection_inside_the_odds_band(self):
        # over 1.5 at 0.87 -> 1.15 is the likeliest thing in the band; the home win (1.33)
        # and double chance 1X (0.91 -> 1.10, so just *below* the floor) both lose to it
        leg = slips.best_leg(football_row())
        self.assertEqual(leg["selection"], "O1.5")
        self.assertAlmostEqual(leg["odds"], 1 / 0.87, places=3)

    def test_a_price_just_under_the_floor_is_excluded(self):
        # the model's 91% double chance is 1.099 - one thousandth below 1.10, and out
        self.assertAlmostEqual(1 / 0.91, 1.0989, places=4)
        leg = slips.best_leg(football_row(double_chance_1x_prob=0.90))  # 1.111, back in band
        self.assertEqual(leg["selection"], "1X")

    def test_selections_above_the_band_are_ignored(self):
        # every outcome is a near-certainty or a long shot - nothing lands in 1.10-1.40
        leg = slips.best_leg(football_row(
            prob_home=0.99, prob_draw=0.005, prob_away=0.005,
            over_1_5_prob=0.99, over_2_5_prob=0.98, over_3_5_prob=0.97, btts_yes_prob=0.5,
            double_chance_1x_prob=0.995, double_chance_12_prob=0.99, double_chance_x2_prob=0.01,
        ))
        self.assertIsNone(leg)

    def test_real_prices_are_used_and_only_where_they_are_offered(self):
        row = football_row()
        prices = {"1X": 1.20, "X2": 3.4, "1": 5.0}  # no 12, no over/under, no BTTS
        leg = slips.best_leg(row, prices)
        self.assertEqual(leg["selection"], "1X")
        self.assertEqual(leg["odds"], 1.20)
        self.assertEqual(leg["odds_source"], "1xlite")

    def test_a_real_price_outside_the_band_is_dropped(self):
        # the app prices the double chance below 1.10, so the next-best in-band real price wins
        leg = slips.best_leg(football_row(), {"1X": 1.05, "12": 1.25})
        self.assertEqual(leg["selection"], "12")
        self.assertEqual(leg["odds_source"], "1xlite")

    def test_tennis_offers_the_two_players_only(self):
        row = football_row(sport="tennis", competition="ATP", home_team="A", away_team="B")
        row.update(prob_home=0.80, prob_draw=0.0, prob_away=0.20)
        leg = slips.best_leg(row)
        self.assertEqual(leg["selection"], "1")
        self.assertEqual(leg["label"], "A to win")
        self.assertEqual(leg["market"], "Match Winner")

    def test_double_chance_falls_back_to_the_plain_sum(self):
        row = football_row()
        row.pop("double_chance_12_prob")
        self.assertAlmostEqual(slips.probability(row, "12"), 0.84, places=6)


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.rows = [football_row(kickoff_hours=h) for h in (6, 30, 54, 78)]

    def slips_in(self, filter_id):
        payload = slips.build_slips(self.rows, now=NOW)
        return next(f for f in payload["filters"] if f["id"] == filter_id)

    def test_the_day_runs_from_0400_to_0400_kathmandu_time(self):
        # 9 Oct 03:00 UTC = 08:45 Kathmandu, so it falls in the day that began 4am Oct 9
        # Kathmandu (8 Oct 22:15 UTC) - not in the cycle that begins 4am Kathmandu Oct 10.
        start = slips.day_start(dt.datetime(2026, 10, 9, 3, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(start, dt.datetime(2026, 10, 8, 22, 15, tzinfo=dt.timezone.utc))

    def test_an_instant_between_midnight_and_4am_kathmandu_belongs_to_the_previous_cycle(self):
        # 8 Oct 22:00 UTC = 9 Oct 03:45 Kathmandu - before 4am, so the day that started
        # 4am Oct 8 Kathmandu (7 Oct 22:15 UTC) is still the current one.
        start = slips.day_start(dt.datetime(2026, 10, 8, 22, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(start, dt.datetime(2026, 10, 7, 22, 15, tzinfo=dt.timezone.utc))

    def test_4am_kathmandu_maps_to_2215_utc_the_previous_day(self):
        self.assertEqual(slips.DAY_START_HOUR, 4)
        self.assertEqual(slips.DAY_TZ.utcoffset(None), dt.timedelta(hours=5, minutes=45))

    def test_a_match_just_before_0400_belongs_to_the_previous_cycle(self):
        rows = [football_row(kickoff_hours=-1)]  # 3:15 Kathmandu, i.e. still Oct 9's early hours
        entry = next(f for f in slips.build_slips(rows, now=NOW)["filters"] if f["id"] == "day_1")
        self.assertEqual(entry["n_available"], 0)

    def test_wider_filters_see_more_of_the_fixtures(self):
        self.assertEqual(self.slips_in("day_1")["n_available"], 1)
        self.assertEqual(self.slips_in("days_2")["n_available"], 2)
        self.assertEqual(self.slips_in("days_4")["n_available"], 4)

    def test_every_day_cycle_is_tagged_with_its_window(self):
        entry = self.slips_in("days_2")
        # 4am Oct 9 and 4am Oct 11, Kathmandu time (= 22:15 UTC the day before each).
        self.assertEqual(entry["start_utc"], "2026-10-08T22:15:00Z")
        self.assertEqual(entry["end_utc"], "2026-10-10T22:15:00Z")


class SlipBuildTest(unittest.TestCase):
    def test_every_slip_lands_inside_the_odds_window(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        payload = slips.build_slips(rows, now=NOW)
        entries = [slip for f in payload["filters"] for slip in f["slips"]]
        self.assertTrue(entries)
        for slip in entries:
            self.assertGreaterEqual(slip["total_odds"], slips.SLIP_ODDS_MIN)
            self.assertLessEqual(slip["total_odds"], slips.SLIP_ODDS_MAX)
            self.assertLessEqual(len(slip["legs"]), slips.MAX_LEGS)

    def test_a_slip_never_uses_the_same_match_twice(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        for slip in slips.build_slips(rows, now=NOW)["filters"][3]["slips"]:
            keys = [leg["match_key"] for leg in slip["legs"]]
            self.assertEqual(len(keys), len(set(keys)))

    def test_with_too_few_matches_no_slip_is_invented(self):
        entry = next(f for f in slips.build_slips([football_row(0, kickoff_hours=6)], now=NOW)["filters"] if f["id"] == "day_1")
        self.assertEqual(entry["slips"], [])

    def test_an_empty_day_still_produces_valid_filters(self):
        payload = slips.build_slips([], now=NOW)
        self.assertEqual([f["id"] for f in payload["filters"]], ["day_1", "days_2", "days_3", "days_4", "days_7", "weeks_2"])
        self.assertTrue(all(f["slips"] == [] and f["main_slip"] is None for f in payload["filters"]))


class MainSlipTest(unittest.TestCase):
    def main_of(self, rows):
        payload = slips.build_slips(rows, now=NOW)
        return next(f for f in payload["filters"] if f["id"] == "day_1")["main_slip"]

    def test_main_slip_respects_both_odds_rules(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(8)]
        main = self.main_of(rows)
        self.assertIsNotNone(main)
        self.assertGreaterEqual(main["total_odds"], slips.MAIN_SLIP_ODDS_MIN)
        self.assertLessEqual(main["total_odds"], slips.MAIN_SLIP_ODDS_MAX)
        for leg in main["legs"]:
            self.assertGreaterEqual(leg["odds"], slips.MAIN_LEG_ODDS_MIN)
            self.assertLessEqual(leg["odds"], slips.MAIN_LEG_ODDS_MAX)

    def test_main_slip_never_reuses_a_match(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(8)]
        keys = [leg["match_key"] for leg in self.main_of(rows)["legs"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_no_main_slip_when_it_cannot_reach_double(self):
        # a single short-priced leg can never total 2.00
        self.assertIsNone(self.main_of([football_row(0, kickoff_hours=6)]))

    def test_a_leg_priced_above_the_main_band_is_never_used(self):
        # every main-slip leg is 1.10-1.30 even though the match also offers a wider leg
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(8)]
        main = self.main_of(rows)
        self.assertTrue(all(leg["odds"] <= slips.MAIN_LEG_ODDS_MAX for leg in main["legs"]))


class FilterWindowTest(unittest.TestCase):
    def test_filters_cover_one_to_four_days_seven_days_and_two_weeks(self):
        payload = slips.build_slips([], now=NOW)
        self.assertEqual([f["day_count"] for f in payload["filters"]], [1, 2, 3, 4, 7, 14])
        self.assertEqual([f["label"] for f in payload["filters"]], ["1 Day", "2 Days", "3 Days", "4 Days", "7 Days", "2 Weeks"])

    def test_the_snapshot_publishes_the_main_slip_ranges(self):
        payload = slips.build_slips([], now=NOW)
        self.assertEqual(payload["main_leg_odds_range"], [1.10, 1.30])
        self.assertEqual(payload["main_slip_odds_range"], [2.00, 3.50])


class WriteTest(unittest.TestCase):
    def test_the_snapshot_is_valid_json_and_has_no_stray_temp_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = slips.write_slips(slips.build_slips([football_row(0, kickoff_hours=6)], now=NOW), Path(tmp.name) / "slips.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["day_start_hour"], 4)
        self.assertEqual(payload["day_start_tz"], "Asia/Kathmandu")
        self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
