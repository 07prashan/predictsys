"""The filter-slip engine: which selection represents a match, how the 04:00 Kathmandu-time
day cycles split the fixtures up, and that a finished slip always lands inside its odds window.
Offline and deterministic - no DB, no network, no clock.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import json
import sqlite3
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


class BetCodeTest(unittest.TestCase):
    """Every slip carries a PSB1- payload that packs the whole slip, so a saved slip can be
    reloaded later - even after the data behind the page changed."""

    def build(self, rows=None):
        rows = rows if rows is not None else [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        return slips.build_slips(rows, now=NOW)

    def test_every_slip_carries_a_payload(self):
        for f in self.build()["filters"]:
            if f["main_slip"]:
                self.assertTrue(f["main_slip"]["code_payload"].startswith(slips.CODE_PREFIX))
            for slip in f["slips"]:
                self.assertTrue(slip["code_payload"].startswith(slips.CODE_PREFIX))

    def test_a_code_decodes_back_to_the_same_slip(self):
        f = next(x for x in self.build()["filters"] if x["id"] == "days_2")
        slip = f["slips"][0]
        decoded = slips.decode_slip_code(slip["code_payload"])
        self.assertEqual(decoded["window"], "2 Days")
        self.assertEqual(decoded["kind"], "combo")
        self.assertEqual(decoded["total_odds"], slip["total_odds"])
        self.assertEqual(len(decoded["legs"]), len(slip["legs"]))
        for got, want in zip(decoded["legs"], slip["legs"]):
            self.assertEqual(got["match_key"], want["match_key"])
            self.assertEqual(got["label"], want["label"])
            self.assertEqual(got["odds"], want["odds"])
            self.assertEqual(got["odds_source"], want["odds_source"])
            self.assertEqual(got["kickoff_utc"], want["kickoff_utc"])

    def test_a_main_slip_code_keeps_its_window_kind_and_rules(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(8)]
        main = next(f for f in self.build(rows)["filters"] if f["id"] == "day_1")["main_slip"]
        decoded = slips.decode_slip_code(main["code_payload"])
        self.assertEqual(decoded["kind"], "main")
        self.assertEqual(decoded["window"], "1 Day")
        self.assertTrue(decoded["legs"])
        self.assertTrue(all(1.10 <= leg["odds"] <= 1.30 for leg in decoded["legs"]))
        self.assertTrue(2.00 <= decoded["total_odds"] <= 3.50)

    def test_a_live_priced_leg_keeps_its_marker_through_the_code(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        odds = {slips.match_key(rows[0]): {"O1.5": "1.15"}}  # the bookmaker offers this one
        live = slips.best_leg(rows[0], odds[slips.match_key(rows[0])])
        model = slips.best_leg(rows[1])
        slip = {"total_odds": round(live["odds"] * model["odds"], 2), "win_prob": 0.5, "legs": [live, model]}
        decoded = slips.decode_slip_code(slips.encode_slip_code(slip, "2 Days", "combo"))
        self.assertEqual(decoded["legs"][0]["odds_source"], "1xlite")
        self.assertEqual(decoded["legs"][1]["odds_source"], "model")
        self.assertEqual(decoded["legs"][0]["odds"], live["odds"])
        self.assertEqual(decoded["legs"][0]["match_key"], live["match_key"])

    def test_unicode_team_names_survive_the_round_trip(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        rows[0]["home_team"] = "Atl\u00e9tico Madrid"
        slip = next(f for f in self.build(rows)["filters"] if f["id"] == "days_4")["slips"][0]
        decoded = slips.decode_slip_code(slip["code_payload"])
        self.assertTrue(any("Atl\u00e9tico" in leg["home_team"] for leg in decoded["legs"]))

    def test_a_damaged_or_foreign_code_is_rejected(self):
        for bad in ("", "hello", "PSB1-%%%%", "PSB1-" + "A" * 40):
            with self.assertRaises(ValueError, msg=bad):
                slips.decode_slip_code(bad)

    def test_the_snapshot_advertises_the_code_prefix(self):
        payload = self.build()
        self.assertEqual(payload["code_prefix"], "PSB1-")
        self.assertEqual(payload["code_length"], 5)
        self.assertEqual((payload["code_alphabet"], len(payload["code_alphabet"])), (slips.CODE_ALPHABET, 32))


class ShortCodeTest(unittest.TestCase):
    """The five-character code a visitor reads out or types back in: unique per slip, stable
    for the same slip, and free of the characters people mix up (0/O, 1/I)."""

    def build(self, rows=None):
        rows = rows if rows is not None else [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        return slips.build_slips(rows, now=NOW)

    @staticmethod
    def every_slip(payload):
        for f in payload["filters"]:
            if f["main_slip"]:
                yield f["label"], f["main_slip"]
            for slip in f["slips"]:
                yield f["label"], slip

    def test_every_slip_gets_five_letters_or_digits(self):
        slips_out = list(self.every_slip(self.build()))
        self.assertTrue(slips_out)
        for _label, slip in slips_out:
            self.assertRegex(slip["code"], r"^[2-9A-HJ-NP-Z]{5}$")

    def test_two_slips_never_share_a_code(self):
        codes = [slip["code"] for _label, slip in self.every_slip(self.build())]
        self.assertEqual(len(codes), len(set(codes)))

    def test_the_same_slip_always_mints_the_same_code(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        first = {(label, tuple(l["match_key"] for l in s["legs"])): s["code"] for label, s in self.every_slip(self.build(rows))}
        second = {(label, tuple(l["match_key"] for l in s["legs"])): s["code"] for label, s in self.every_slip(self.build(rows))}
        self.assertEqual(first, second)

    def test_a_different_slip_gets_a_different_code(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(12)]
        one = next(f for f in self.build(rows)["filters"] if f["id"] == "day_1")["slips"][0]
        two = next(f for f in self.build(rows)["filters"] if f["id"] == "days_2")["slips"][0]
        self.assertNotEqual(one["code"], two["code"])

    def test_the_code_avoids_lookalike_characters(self):
        for char in "01OI":
            self.assertNotIn(char, slips.CODE_ALPHABET)


class FilterWindowTest(unittest.TestCase):
    def test_filters_cover_one_to_four_days_seven_days_and_two_weeks(self):
        payload = slips.build_slips([], now=NOW)
        self.assertEqual([f["day_count"] for f in payload["filters"]], [1, 2, 3, 4, 7, 14])
        self.assertEqual([f["label"] for f in payload["filters"]], ["1 Day", "2 Days", "3 Days", "4 Days", "7 Days", "2 Weeks"])

    def test_the_snapshot_publishes_the_main_slip_ranges(self):
        payload = slips.build_slips([], now=NOW)
        self.assertEqual(payload["main_leg_odds_range"], [1.10, 1.30])
        self.assertEqual(payload["main_slip_odds_range"], [2.00, 3.50])


class ConsistencyTest(unittest.TestCase):
    """Among legs the model likes about equally, the slip prefers the side that has been
    winning its last five - the top flight's consistently performing clubs."""

    @staticmethod
    def backing(i, form, prob_home=0.85, **overrides):
        """A row whose best in-band selection is the home win, so the leg backs Home i."""
        return football_row(
            i, prob_home=prob_home, prob_draw=0.10, prob_away=0.05,
            double_chance_1x_prob=0.95, double_chance_12_prob=0.60, double_chance_x2_prob=0.15,
            over_1_5_prob=0.80, over_2_5_prob=0.60, over_3_5_prob=0.35, btts_yes_prob=0.45,
            home_form=form, away_form="LLLLL", home_rating=1620, away_rating=1450, **overrides
        )

    def slip(self, rows):
        result = slips.build_filter(rows, prices={}, now=NOW, day_count=1)
        return next(s for s in result["slips"] if s["target"] == 2.0)

    def test_a_result_leg_records_the_side_it_backs(self):
        leg = slips.best_leg(self.backing(0, "WWDLW"))
        self.assertEqual(leg["selection"], "1")
        self.assertEqual((leg["backed_team"], leg["backed_form"], leg["backed_rating"]), ("Home 0", "WWDLW", 1620))

    def test_an_away_selection_backs_the_away_side(self):
        row = football_row(0, prob_home=0.05, prob_draw=0.10, prob_away=0.85, away_form="WLWWW", away_rating=1640,
                          over_1_5_prob=0.80, over_2_5_prob=0.60, btts_yes_prob=0.45)
        leg = slips.best_leg(row)
        self.assertEqual(leg["selection"], "2")
        self.assertEqual((leg["backed_team"], leg["backed_form"], leg["backed_rating"]), ("Away 0", "WLWWW", 1640))

    def test_a_totals_leg_backs_nobody(self):
        leg = slips.best_leg(football_row(0, home_form="WWWWW", away_form="LLLLL"))
        self.assertEqual(leg["selection"], "O1.5")
        self.assertEqual((leg["backed_team"], leg["backed_form"], leg["backed_rating"]), (None, None, None))

    def test_form_wins_reads_the_most_recent_five(self):
        self.assertEqual(slips.form_wins("WWWWW"), 5)
        self.assertEqual(slips.form_wins("LDWWL"), 2)
        self.assertEqual(slips.form_wins("LLWWWWWW"), 5)  # only the last five count
        self.assertIsNone(slips.form_wins(""))
        self.assertIsNone(slips.form_wins(None))

    def test_a_five_win_side_edges_out_a_marginally_more_likely_side_out_of_form(self):
        rows = [self.backing(i, "WWWWW") if i % 2 else self.backing(i, "LLLLL", prob_home=0.87) for i in range(6)]
        legs = self.slip(rows)["legs"]
        # the slip fills up to its target, and every five-win leg is taken before any of the
        # marginally likelier out-of-form ones
        self.assertEqual([leg["backed_form"] for leg in legs], ["WWWWW"] * 3 + ["LLLLL"] * 2)

    def test_the_nudge_never_displaces_a_clearly_safer_leg(self):
        # 0.90 x 1.015 = 0.914 still beats 0.78 x 1.075 = 0.839
        rows = [self.backing(0, "WWWWW", prob_home=0.78)] + [self.backing(i, "LLLLL", prob_home=0.90) for i in range(1, 6)]
        legs = self.slip(rows)["legs"]
        self.assertEqual(legs[0]["prob"], 0.90)
        self.assertEqual(legs[-1]["home_team"], "Home 0")


class TopTierOnlyTest(unittest.TestCase):
    """Slip legs come only from top-tier football - the world's top flights and the
    continental competitions, never the second divisions."""

    def build(self, rows):
        return slips.build_filter(rows, prices={}, now=NOW, day_count=1)

    def all_legs(self, result):
        legs = [leg for slip in result["slips"] for leg in slip["legs"]]
        if result["main_slip"]:
            legs += result["main_slip"]["legs"]
        return legs

    def test_second_division_rows_never_become_legs(self):
        # eight perfect second-division favourites - not one of them may reach a slip
        result = self.build([football_row(i, league="E1", competition="Championship") for i in range(8)])
        self.assertEqual(result["n_available"], 0)
        self.assertEqual(result["slips"], [])
        self.assertIsNone(result["main_slip"])

    def test_top_flights_world_leagues_and_continental_ties_are_all_leg_material(self):
        for code in ("UCL", "UEL", "UECL", "ACLE", "ACL2", "LIB", "SUD", "CCC", "CAFCL",
                     "E0", "SP1", "D1", "I1", "F1", "P1", "N1", "T1", "B1", "SC0",
                     "BRA", "ARG", "MEX", "USA", "JPN", "CHN"):
            self.assertEqual(self.build([football_row(0, league=code)])["n_available"], 1, code)

    def test_second_divisions_are_the_only_football_competitions_left_out(self):
        for code in sorted(slips.NON_TOP_TIER_LEAGUES):
            self.assertEqual(
                self.build([football_row(0, league=code), football_row(1, league="E0")])["n_available"], 1, code
            )

    def test_no_second_division_ever_reaches_a_finished_slip(self):
        rows = [football_row(i, kickoff_hours=6 + i, league="E0") for i in range(6)]
        rows += [football_row(50 + i, kickoff_hours=6 + i, league="E1") for i in range(6)]
        result = self.build(rows)
        legs = self.all_legs(result)
        self.assertTrue(legs)
        self.assertEqual({leg["league"] for leg in legs}, {"E0"})

    def test_a_second_division_row_still_loses_even_when_it_would_fund_the_whole_slip(self):
        rows = [football_row(i, league="E0") for i in range(6)] + [football_row(9, league="SP2", prob_home=0.98)]
        result = self.build(rows)
        self.assertTrue(result["slips"])
        for slip in result["slips"]:
            self.assertNotIn("SP2", {leg["league"] for leg in slip["legs"]})


class BoardTest(unittest.TestCase):
    """The bookmaker board: every market the model can honestly price for one fixture, per
    sport - what the page's slip editor offers when you switch a leg's market."""

    def codes(self, row, prices=None):
        return {entry["code"]: entry for entry in slips.board(row, prices)}

    def test_a_football_row_prices_the_stored_board_and_the_derived_one(self):
        row = football_row(0, expected_goals_home=2.1, expected_goals_away=0.8)
        codes = self.codes(row)
        # the stored board the pipeline already computes...
        for code in ("1", "X", "2", "1X", "12", "X2", "O1.5", "O2.5", "U3.5", "BTTS", "BTTS_NO"):
            self.assertIn(code, codes)
        # ...and the markets derived from this row's expected goals
        for code in ("O0.5", "EX2", "EX3", "T1O1.5", "T2O1.5", "T1E2", "AH1", "AH2", "1+O2.5", "2+O2.5"):
            self.assertIn(code, codes)
        # every selection carries what the editor and the card show
        for entry in codes.values():
            self.assertTrue({"code", "market", "label", "group", "prob", "odds", "odds_source"} <= set(entry))
            self.assertGreater(entry["prob"], 0)
            self.assertLess(entry["prob"], 1)

    def test_a_row_without_expected_goals_still_prices_the_stored_board(self):
        codes = self.codes(football_row(0))
        self.assertIn("O2.5", codes)
        self.assertNotIn("AH1", codes)  # nothing is derived from a column the row doesn't have

    def test_derived_probabilities_behave_like_a_goal_model(self):
        probs = slips.derived_probabilities(football_row(0, expected_goals_home=1.4, expected_goals_away=1.1))
        self.assertAlmostEqual(probs["O0.5"] + probs["U0.5"], 1.0, places=6)
        self.assertGreater(probs["O1.5"], probs["O2.5"])
        self.assertGreater(probs["O2.5"], probs["O3.5"])
        self.assertGreater(probs["O3.5"], probs["O4.5"])
        self.assertGreater(probs["T1O1.5"], probs["T2O1.5"])  # the home side is the stronger one
        self.assertLess(probs["AH1"] + probs["AH2"], 1.0)
        self.assertGreater(probs["EX2"] + probs["EX3"], 0.3)
        self.assertGreater(probs["1+O2.5"], probs["2+O2.5"])

    def test_basketball_prices_its_moneyline_and_the_one_total_it_models(self):
        row = {"league": "NBA", "sport": "basketball", "competition": "NBA Preseason",
               "home_team": "Dallas Mavericks", "away_team": "Houston Rockets",
               "prob_home": 0.72, "prob_draw": 0.0, "prob_away": 0.28,
               "total_line": 224.5, "over_prob": 0.55, "under_prob": 0.45}
        codes = self.codes(row)
        self.assertEqual(codes["1"]["prob"], 0.72)
        self.assertEqual(codes["1"]["label"], "Dallas Mavericks to Win")
        self.assertEqual(codes["O224.5"]["prob"], 0.55)
        self.assertEqual(codes["U224.5"]["prob"], 0.45)
        self.assertEqual(codes["O224.5"]["market"], "Total Points")

    def test_every_sport_names_its_shelves_the_way_its_own_page_does(self):
        basketball = {"league": "NBA", "sport": "basketball", "home_team": "Dallas Mavericks",
                      "away_team": "Houston Rockets", "prob_home": 0.72, "prob_draw": 0.0,
                      "prob_away": 0.28, "total_line": 224.5, "over_prob": 0.55, "under_prob": 0.45}
        codes = self.codes(basketball)
        self.assertEqual(codes["1"]["group"], "Moneyline")        # not football's "Result"
        self.assertEqual(codes["O224.5"]["group"], "Total Points")  # not the unnamed "Markets"
        tennis = {"league": "ATP", "sport": "tennis", "home_team": "Carlos Alcaraz",
                  "away_team": "Matteo Arnaldi", "prob_home": 0.8, "prob_draw": 0.0, "prob_away": 0.2}
        self.assertEqual(self.codes(tennis)["1"]["group"], "Match Winner")
        cricket = {"league": "T20", "sport": "cricket", "home_team": "India",
                   "away_team": "Australia", "prob_home": 0.74, "prob_draw": 0.0, "prob_away": 0.26}
        self.assertEqual(self.codes(cricket)["1"]["group"], "Match Winner")

    def test_tennis_prices_the_winner_and_the_sets_markets(self):
        row = {"league": "ATP", "sport": "tennis", "competition": "China Open",
               "home_team": "Carlos Alcaraz", "away_team": "Matteo Arnaldi",
               "prob_home": 0.8, "prob_draw": 0.0, "prob_away": 0.2,
               "goes_the_distance_prob": 0.35, "sets_line": 2.5, "sets_over_prob": 0.6}
        codes = self.codes(row)
        self.assertEqual(set(codes), {"1", "2", "DIST", "SETS_O2.5", "SETS_U2.5"})
        self.assertEqual(codes["SETS_U2.5"]["prob"], 0.4)

    def test_a_cricket_style_row_still_gets_a_match_winner_board(self):
        row = {"league": "T20", "sport": "cricket", "home_team": "India", "away_team": "Australia",
               "prob_home": 0.74, "prob_draw": 0.0, "prob_away": 0.26}
        codes = self.codes(row)
        self.assertEqual(codes["1"]["prob"], 0.74)
        self.assertEqual(codes["1"]["label"], "India to Win")

    def test_real_prices_still_win_and_still_only_cover_what_the_app_quotes(self):
        codes = self.codes(football_row(0), {"1X": 1.2, "O2.5": 1.9})
        self.assertEqual(set(codes), {"1X", "O2.5"})
        self.assertEqual(codes["1X"]["odds_source"], "1xlite")


class ProbabilityFloorTest(unittest.TestCase):
    """A leg has to be genuinely likely, not merely cheap: the odds band alone lets a
    bookmaker's margin put a short-priced pick on a slip the model does not believe in."""

    def test_a_selection_inside_the_odds_band_but_below_the_floor_is_never_a_leg(self):
        row = football_row(0, over_2_5_prob=0.66)
        self.assertIsNone(slips.best_leg(row, {"O2.5": 1.35}))  # real price in band, 66% model

    def test_the_same_selection_qualifies_once_it_clears_the_floor(self):
        leg = slips.best_leg(football_row(0, over_2_5_prob=0.80), {"O2.5": 1.35})
        self.assertEqual(leg["selection"], "O2.5")

    def test_a_real_price_in_the_headline_band_still_has_to_clear_its_stricter_floor(self):
        # 1.20 sits inside the headline band while the model rates the pick 72%: good enough
        # for a combination slip, not for the headline one
        row = football_row(0, over_2_5_prob=0.72)
        main = slips.best_leg(row, {"O2.5": 1.20}, slips.MAIN_LEG_ODDS_MIN,
                              slips.MAIN_LEG_ODDS_MAX, slips.MAIN_LEG_PROB_MIN)
        self.assertIsNone(main)
        self.assertEqual(slips.best_leg(row, {"O2.5": 1.20})["selection"], "O2.5")

    def test_a_window_of_seventy_percent_picks_gets_no_headline_slip(self):
        rows = [football_row(i, kickoff_hours=6 + i, over_2_5_prob=0.72, prob_home=0.72,
                             prob_draw=0.1, prob_away=0.18, double_chance_1x_prob=0.72,
                             double_chance_12_prob=0.72, over_1_5_prob=0.72, btts_yes_prob=0.72)
                for i in range(6)]
        entry = slips.build_filter(rows, prices={}, now=NOW, day_count=1)
        self.assertIsNone(entry["main_slip"])
        self.assertTrue(entry["slips"])  # the wider 70% floor still funds a combination slip


class MultipleSlipsTest(unittest.TestCase):
    """One headline slip plus up to five combinations, each landing in the 2.00-3.00 window
    the page promises - and never the same bet twice."""

    def test_a_window_publishes_no_more_than_five_combination_slips(self):
        rows = [football_row(i, kickoff_hours=6 + i, over_2_5_prob=0.7 + (i % 5) * 0.04) for i in range(14)]
        entry = next(f for f in slips.build_slips(rows, now=NOW)["filters"] if f["id"] == "weeks_2")
        self.assertTrue(entry["main_slip"])
        self.assertGreaterEqual(len(entry["slips"]), 2)
        self.assertLessEqual(len(entry["slips"]), slips.MAX_SLIPS)
        for slip in entry["slips"]:
            self.assertGreaterEqual(slip["total_odds"], slips.SLIP_ODDS_MIN)
            self.assertLessEqual(slip["total_odds"], slips.SLIP_ODDS_MAX)

    def test_no_two_combination_slips_are_the_same_bet(self):
        rows = [football_row(i, kickoff_hours=6 + i, over_2_5_prob=0.7 + (i % 5) * 0.04) for i in range(14)]
        entry = next(f for f in slips.build_slips(rows, now=NOW)["filters"] if f["id"] == "weeks_2")
        signatures = [frozenset(leg["match_key"] for leg in slip["legs"]) for slip in entry["slips"]]
        self.assertEqual(len(signatures), len(set(signatures)))

    def test_the_snapshot_publishes_its_own_slip_rules(self):
        payload = slips.build_slips([football_row(0, kickoff_hours=6)], now=NOW)
        self.assertEqual(payload["max_slips"], 5)
        self.assertEqual(payload["min_leg_prob"], 0.70)
        self.assertEqual(payload["slip_odds_range"], [2.0, 3.0])


class EligibilityTagTest(unittest.TestCase):
    """site_export decides who may go on a slip (big competitions always, smaller leagues only
    with a club in Europe); the tag is authoritative when it is there."""

    def test_a_fixture_the_export_tagged_out_is_never_a_leg(self):
        out = football_row(0, league="DNK")
        out["slip_eligible"] = False
        kept = football_row(1, league="DNK")
        kept["slip_eligible"] = True
        entry = slips.build_filter([out, kept], prices={}, now=NOW, day_count=1)
        self.assertEqual(entry["n_available"], 1)  # only the tagged-in fixture is available
        self.assertTrue(all(leg["home_team"] == "Home 1"
                            for slip in entry["slips"] for leg in slip["legs"]))

    def test_without_the_tag_the_old_rule_stands(self):
        self.assertFalse(slips.eligible(football_row(0, league="E1")))
        self.assertTrue(slips.eligible(football_row(0, league="E0")))
        self.assertTrue(slips.eligible({"league": "NBA", "sport": "basketball"}))


class InsightTest(unittest.TestCase):
    """Every leg explains itself from the fixture's own record: the two sides' last games and
    their last meeting - and falls back to the model's number when the database has none."""

    @staticmethod
    def game(gf, ga, outcome):
        return {"date": "2026-10-01", "opponent": "Someone", "venue": "H",
                "goals_for": gf, "goals_against": ga, "outcome": outcome}

    def context(self):
        return {
            "teams": {
                "Home 0": [self.game(2, 1, "W"), self.game(3, 0, "W"), self.game(1, 0, "W"),
                           self.game(2, 2, "D"), self.game(4, 1, "W")],
                "Away 0": [self.game(1, 2, "L"), self.game(0, 1, "L"), self.game(2, 2, "D"),
                           self.game(1, 0, "W"), self.game(0, 3, "L")],
            },
            "h2h": {"T1|Home 0|Away 0": {"summary": {"home_wins": 3, "away_wins": 1, "draws": 1},
                                          "total": 5,
                                          "meetings": [{"date": "2026-04-01", "home_team": "Home 0",
                                                        "away_team": "Away 0", "home_goals": 3,
                                                        "away_goals": 1}]}},
        }

    def test_a_result_leg_quotes_the_side_s_form_and_the_last_meeting(self):
        # six fixtures so the headline slip can actually reach double money
        rows = [football_row(i, kickoff_hours=6 + i, prob_home=0.85, prob_draw=0.06,
                             prob_away=0.09, over_1_5_prob=0.72) for i in range(6)]
        entry = slips.build_filter(rows, prices={}, now=NOW, day_count=1, context=self.context())
        leg = entry["main_slip"]["legs"][0]
        self.assertEqual(leg["selection"], "1")
        self.assertIn("Home 0 have won 4 of their last 5", leg["insight"])
        self.assertIn("3-1", leg["insight"])

    def test_a_totals_leg_quotes_both_sides_scoring_record(self):
        row = football_row(0, over_1_5_prob=0.87)
        leg = slips.best_leg(row)
        text = slips.insight_for(row, leg, self.context())
        self.assertIn("Over 1.5 landed in", text)
        self.assertIn("Away 0", text)

    def test_a_both_teams_to_score_leg_quotes_how_often_each_side_saw_it(self):
        row = football_row(0)
        leg = {"match_key": "T1|Home 0|Away 0", "label": "Both Teams to Score", "group": "Both Teams to Score",
               "selection": "BTTS", "prob": 0.53, "odds": 1.89}
        text = slips.insight_for(row, leg, self.context())
        self.assertIn("Both teams scored in 3 of Home 0's last 5 and 2 of Away 0's last 5", text)
        self.assertIn("3-1", text)

    def test_a_leg_without_any_history_falls_back_to_the_models_number(self):
        leg = slips.best_leg(football_row(0))
        text = slips.insight_for(football_row(0), leg, None)
        self.assertIn("The model has", text)
        self.assertIn("%", text)

    def test_a_winner_reason_quotes_the_side_the_leg_actually_backs(self):
        # an away win must quote the away side's record - the home side's form is no reason
        row = {"league": "ATP", "sport": "tennis", "home_team": "Home 0", "away_team": "Away 0",
               "prob_home": 0.2, "prob_draw": 0.0, "prob_away": 0.8}
        leg = {"match_key": "ATP|Home 0|Away 0", "label": "Away 0 to win", "group": "Match Winner",
               "selection": "2", "prob": 0.8, "odds": 1.25}
        text = slips.insight_for(row, leg, self.context())
        self.assertIn("Away 0 won 1 of their last 5 (LWDLL)", text)
        self.assertNotIn("Home 0 won", text)

    def test_a_form_reason_counts_scoring_games_instead_of_claiming_them_all(self):
        row = football_row(0, prob_home=0.1, prob_draw=0.15, prob_away=0.75)
        leg = {"match_key": "T1|Home 0|Away 0", "label": "Away Win", "group": "Result",
               "selection": "2", "prob": 0.75, "odds": 1.33}
        text = slips.insight_for(row, leg, self.context())
        # Away 0 scored in 3 of their last 5 (1, 0, 2, 1, 0) - never "every one of them"
        self.assertIn("scored in 3 of them", text)
        self.assertNotIn("every one", text)

    def test_a_basketball_total_quotes_the_model_projection(self):
        row = {"league": "NBA", "sport": "basketball", "home_team": "Home 0", "away_team": "Away 0",
               "prob_home": 0.6, "prob_draw": 0.0, "prob_away": 0.4,
               "total_line": 224.5, "over_prob": 0.55, "under_prob": 0.45, "predicted_total": 227.4}
        leg = {"match_key": "NBA|Home 0|Away 0", "label": "Over 224.5 Points", "group": "Total Points",
               "selection": "O224.5", "prob": 0.55, "odds": 1.818}
        text = slips.insight_for(row, leg, self.context())
        self.assertIn("projects 227 points against a 224.5 line", text)

    def test_a_sets_reason_quotes_both_players_records(self):
        row = {"league": "ATP", "sport": "tennis", "home_team": "Home 0", "away_team": "Away 0",
               "prob_home": 0.6, "prob_draw": 0.0, "prob_away": 0.4,
               "goes_the_distance_prob": 0.4, "sets_line": 2.5, "sets_over_prob": 0.6}
        leg = {"match_key": "ATP|Home 0|Away 0", "label": "Over 2.5 Sets", "group": "Sets",
               "selection": "SETS_O2.5", "prob": 0.6, "odds": 1.667}
        text = slips.insight_for(row, leg, self.context())
        self.assertIn("Home 0 won 4 of their last 5", text)
        self.assertIn("Away 0 won 1 of their last 5", text)

    def test_every_leg_on_a_built_slip_carries_a_reason(self):
        rows = [football_row(i, kickoff_hours=6 + i) for i in range(8)]
        entry = slips.build_filter(rows, prices={}, now=NOW, day_count=1, context=self.context())
        legs = list(entry["main_slip"]["legs"]) if entry["main_slip"] else []
        for slip in entry["slips"]:
            legs += slip["legs"]
        self.assertTrue(legs)
        self.assertTrue(all(leg.get("insight") for leg in legs))


class PoolTest(unittest.TestCase):
    """The fixture pool the page's editor works from: every fixture it may add, with its whole
    board and one reason-line per market shelf."""

    @staticmethod
    def rows(count=10):
        return [football_row(i, kickoff_hours=6 + i, expected_goals_home=1.8, expected_goals_away=0.9)
                for i in range(count)]

    def test_the_snapshot_carries_a_pool_of_editable_fixtures(self):
        payload = slips.build_slips(self.rows(), now=NOW)
        self.assertTrue(payload["pool"])
        entry = payload["pool"][0]
        self.assertTrue(entry["markets"])
        for market in entry["markets"]:
            self.assertIn(market["group"], entry["insights"])
        self.assertTrue(all(key in entry for key in
                            ("match_key", "league", "sport", "kickoff_utc", "home_team", "away_team")))

    def test_every_published_leg_can_be_edited_against_the_pool(self):
        payload = slips.build_slips(self.rows(14), now=NOW)
        keys = {entry["match_key"] for entry in payload["pool"]}
        for f in payload["filters"]:
            for slip in ([f["main_slip"]] if f["main_slip"] else []) + list(f["slips"]):
                for leg in slip["legs"]:
                    self.assertIn(leg["match_key"], keys)

    def test_a_leg_from_an_editable_pool_leg_keeps_its_market_and_reason(self):
        payload = slips.build_slips(self.rows(), now=NOW)
        entry = payload["pool"][0]
        market = next(m for m in entry["markets"] if m["group"] in entry["insights"])
        self.assertEqual(entry["insights"][market["group"]],
                         slips.insight_for(entry, {"match_key": entry["match_key"], "label": market["label"],
                                                   "group": market["group"], "prob": market["prob"],
                                                   "odds": market["odds"], "selection": market["code"]}, None))


class DailyCodeTest(unittest.TestCase):
    """One code per slip per betting day: minted once, reused for every build that day, and
    reset together at 4am Kathmandu - so a copied code keeps naming the same slip."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    @staticmethod
    def codes_in(payload):
        return [slip["code"] for f in payload["filters"]
                for slip in ([f["main_slip"]] if f["main_slip"] else []) + list(f["slips"])]

    @staticmethod
    def rows(count=8):
        return [football_row(i, kickoff_hours=6 + i) for i in range(count)]

    def test_the_betting_day_turns_over_at_4am_kathmandu(self):
        # 8 Oct 22:14 UTC is 03:59 in Kathmandu - still the old betting day...
        before = slips.day_bucket(dt.datetime(2026, 10, 8, 22, 14, tzinfo=dt.timezone.utc))
        # ...and a minute later the clock reads 4:00, which is where a betting day begins
        after = slips.day_bucket(dt.datetime(2026, 10, 8, 22, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(after, before + 1)
        self.assertEqual(slips.day_bucket(NOW), slips.day_bucket(slips.day_start(NOW)))
        self.assertNotEqual(slips.day_bucket(NOW), slips.day_bucket(slips.day_start(NOW) - dt.timedelta(minutes=1)))

    def test_a_slot_keeps_its_code_for_the_whole_day(self):
        first = slips.DailyCodes(self.conn, NOW).code_for("1 Day|main|0")
        self.assertEqual(len(first), slips.CODE_LENGTH)
        self.assertTrue(set(first) <= set(slips.CODE_ALPHABET))
        # a later build the same day (a 2-hourly refresh) reads the same code back
        later = slips.DailyCodes(self.conn, NOW + dt.timedelta(hours=6))
        self.assertEqual(later.code_for("1 Day|main|0"), first)

    def test_the_next_betting_day_mints_a_fresh_code_and_drops_yesterdays(self):
        today = slips.DailyCodes(self.conn, NOW).code_for("1 Day|main|0")
        tomorrow = slips.DailyCodes(self.conn, NOW + dt.timedelta(days=1))
        fresh = tomorrow.code_for("1 Day|main|0")
        self.assertNotEqual(fresh, today)
        rows = self.conn.execute("SELECT day_bucket, slot FROM slip_codes").fetchall()
        self.assertEqual(rows, [(tomorrow.bucket, "1 Day|main|0")])  # yesterday's row is gone

    def test_two_slips_never_share_a_code_on_the_same_day(self):
        codes = slips.DailyCodes(self.conn, NOW)
        minted = {codes.code_for(f"1 Day|combo|{i}") for i in range(50)}
        self.assertEqual(len(minted), 50)

    def test_a_rebuilt_slip_keeps_its_code_even_when_the_price_moves(self):
        # the reason codes are looked up rather than hashed: 1xLite repricing the same slip
        # must not silently invalidate the code someone copied this morning
        first = slips.build_slips(self.rows(), now=NOW, codes=slips.DailyCodes(self.conn, NOW))
        repriced = slips.build_slips(
            [football_row(i, kickoff_hours=6 + i, over_1_5_prob=0.80) for i in range(8)],
            now=NOW + dt.timedelta(hours=3), codes=slips.DailyCodes(self.conn, NOW + dt.timedelta(hours=3)))
        for a, b in zip(first["filters"], repriced["filters"]):
            if not (a["main_slip"] and b["main_slip"]):
                continue
            self.assertEqual(a["main_slip"]["code"], b["main_slip"]["code"])
        self.assertTrue(any(a["main_slip"] and b["main_slip"] and
                            a["main_slip"]["total_odds"] != b["main_slip"]["total_odds"]
                            for a, b in zip(first["filters"], repriced["filters"])))  # prices really did move

    def test_every_code_in_a_snapshot_is_unique(self):
        payload = slips.build_slips(self.rows(20), now=NOW, codes=slips.DailyCodes(self.conn, NOW))
        codes = self.codes_in(payload)
        self.assertTrue(codes)
        self.assertEqual(len(codes), len(set(codes)))


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
