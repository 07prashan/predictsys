"""The pure logic behind the national-team, tennis and club-name-matching code: the
set-score model, rating updates, name handling. Offline, no fixtures needed.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import intl  # noqa: E402
import tennis  # noqa: E402
from team_match import match_club, match_clubs  # noqa: E402


class SetScoreModelTest(unittest.TestCase):
    def test_distribution_is_a_probability_distribution(self):
        for best_of, outcomes in ((3, 4), (5, 6)):
            for p in (0.5, 0.65, 0.9, 0.02):
                dist = tennis.set_score_probs(p, best_of)
                self.assertEqual(len(dist), outcomes)
                self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)

    def test_it_reproduces_the_match_probability_it_was_built_from(self):
        for best_of in (3, 5):
            for p in (0.3, 0.5, 0.8, 0.95):
                dist = tennis.set_score_probs(p, best_of)
                need = best_of // 2 + 1
                self.assertAlmostEqual(sum(v for (a, _), v in dist.items() if a == need), p, places=6)

    def test_a_coin_flip_match(self):
        m = tennis.sets_markets(0.5, 3)
        self.assertAlmostEqual(m["straight_sets_prob"], 0.5)
        self.assertAlmostEqual(m["goes_the_distance_prob"], 0.5)
        self.assertEqual(m["sets_line"], 2.5)

    def test_favourites_win_in_straight_sets_more_often_and_best_of_five_has_a_higher_line(self):
        self.assertGreater(tennis.sets_markets(0.9, 3)["straight_sets_prob"], tennis.sets_markets(0.55, 3)["straight_sets_prob"])
        self.assertEqual(tennis.sets_markets(0.7, 5)["sets_line"], 3.5)
        best = tennis.sets_markets(0.97, 3)
        self.assertEqual((best["correct_score_home"], best["correct_score_away"]), (2, 0))

    def test_score_parsing_counts_only_finished_sets(self):
        self.assertEqual(tennis._sets_won("6-4 6-3"), (2, 0))
        self.assertEqual(tennis._sets_won("7-6(4) 3-6 6-2"), (2, 1))
        self.assertEqual(tennis._sets_won("6-4 3-1 RET"), (1, 0))  # the unfinished set isn't anyone's
        self.assertEqual(tennis._sets_won("W/O"), (0, 0))


class TennisRatingsTest(unittest.TestCase):
    def test_player_identity_ignores_order_accents_and_punctuation(self):
        self.assertEqual(tennis.player_key("Zhou Yi"), tennis.player_key("Yi Zhou"))
        self.assertEqual(tennis.player_key("Jiří Lehečka"), tennis.player_key("Jiri Lehecka"))
        self.assertEqual(tennis.player_key("Lloyd Harris-Smith"), tennis.player_key("lloyd harris smith"))
        self.assertNotEqual(tennis.player_key("Carlos Alcaraz"), tennis.player_key("Carlos Alcaraz Garfia"))

    def test_surface_is_read_off_the_tournament_name(self):
        april, june = pd.Timestamp("2026-04-20"), pd.Timestamp("2026-06-10")
        self.assertEqual(tennis.surface_of("Wimbledon", june), "Grass")
        self.assertEqual(tennis.surface_of("Internazionali BNL d'Italia (Rome)", april), "Clay")
        self.assertEqual(tennis.surface_of("Rolex Paris Masters", april), "Hard")
        self.assertEqual(tennis.surface_of("Stuttgart Open", june), "Grass")
        self.assertEqual(tennis.surface_of("Porsche Tennis Grand Prix Stuttgart", april), "Clay")

    def test_a_win_moves_rating_up_for_the_winner_and_down_for_the_loser(self):
        r = tennis.Ratings()
        r.update("a", "b", "Hard", pd.Timestamp("2026-01-01"))
        self.assertGreater(r.overall["a"], tennis.INITIAL_RATING)
        self.assertLess(r.overall["b"], tennis.INITIAL_RATING)
        # equal experience, so the two sides move by exactly the same amount
        self.assertAlmostEqual(r.overall["a"] - 1500, 1500 - r.overall["b"])
        self.assertEqual(r.surface["Clay"]["a"], tennis.INITIAL_RATING)  # a hard-court win says nothing about clay
        self.assertEqual("".join(r.form["a"]), "W")
        self.assertEqual("".join(r.form["b"]), "L")

    def test_unknown_players_are_rated_below_average_and_equal_players_are_a_coin_flip(self):
        r = tennis.Ratings()
        self.assertEqual(r.rating("nobody", "Hard"), tennis.UNKNOWN_PLAYER_RATING)
        self.assertAlmostEqual(tennis.win_probability(1700, 1700, 3), 0.5)
        self.assertGreater(tennis.win_probability(1800, 1700, 5), tennis.win_probability(1800, 1700, 3))  # best-of-5 favours the better player


class NationalTeamTest(unittest.TestCase):
    def test_tournament_weights(self):
        self.assertEqual(intl.tournament_weight("Friendly"), 20)
        self.assertEqual(intl.tournament_weight("FIFA World Cup"), 60)
        self.assertEqual(intl.tournament_weight("UEFA Euro"), 50)
        self.assertEqual(intl.tournament_weight("UEFA Nations League"), 40)
        self.assertEqual(intl.tournament_weight("FIFA World Cup qualification"), 40)
        self.assertEqual(intl.tournament_weight("ASEAN Championship"), 30)

    def test_espn_spellings_map_onto_the_datasets(self):
        self.assertEqual(intl.canonical("Czechia"), "Czech Republic")
        self.assertEqual(intl.canonical("Türkiye"), "Turkey")
        self.assertEqual(intl.canonical("Spain"), "Spain")

    def _rate(self, rows):
        frame = pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_goals", "away_goals", "tournament", "neutral"])
        frame["date"] = pd.to_datetime(frame["date"])
        return intl.rate_matches(frame)

    def test_ratings_are_zero_sum_and_a_bigger_win_moves_them_further(self):
        _, narrow, _ = self._rate([("2026-01-01", "A", "B", 1, 0, "Friendly", True)])
        _, big, _ = self._rate([("2026-01-01", "A", "B", 5, 0, "Friendly", True)])
        for ratings in (narrow, big):
            self.assertAlmostEqual(ratings["A"] + ratings["B"], 2 * intl.INITIAL_RATING)
        self.assertGreater(big["A"], narrow["A"])

    def test_home_advantage_applies_unless_the_venue_is_neutral(self):
        _, home_draw, _ = self._rate([("2026-01-01", "A", "B", 1, 1, "Friendly", False)])
        _, neutral_draw, _ = self._rate([("2026-01-01", "A", "B", 1, 1, "Friendly", True)])
        self.assertLess(home_draw["A"], intl.INITIAL_RATING)  # a home side is expected to win, so a draw costs it
        self.assertEqual(neutral_draw["A"], intl.INITIAL_RATING)

    def test_form_is_the_last_five_oldest_first(self):
        # A scores 1,0,0,1,1,0,1 against a B that never scores: W D D W W D W for A, L D D L L D L for B
        rows = [(f"2026-01-{d:02d}", "A", "B", g, 0, "Friendly", True) for d, g in zip(range(1, 8), (1, 0, 0, 1, 1, 0, 1))]
        _, _, form = self._rate(rows)
        self.assertEqual(form["A"], "DWWDW")
        self.assertEqual(form["B"], "DLLDL")

    def test_expected_goals_are_symmetric_at_equal_strength(self):
        coef = np.array([0.1, 0.5, 0.0])
        lam_h, lam_a = intl.expected_goals(coef, [1700.0], [1700.0], [True])
        self.assertAlmostEqual(float(lam_h[0]), float(lam_a[0]))
        lam_h, lam_a = intl.expected_goals(coef, [1900.0], [1600.0], [False])
        self.assertGreater(float(lam_h[0]), float(lam_a[0]))

    def test_only_plain_full_time_internationals_become_history(self):
        base = {"kind": "intl", "state": "post", "status": "STATUS_FULL_TIME", "competition": "UEFA Nations League",
                "kickoff_utc": pd.Timestamp("2026-09-10T18:45:00Z"), "home_goals": 2, "away_goals": 1}
        rows = [
            {**base, "external_id": "ok", "home": "Czechia", "away": "Spain"},
            {**base, "external_id": "extra-time", "status": "STATUS_FINAL_AET", "home": "A", "away": "B"},
            {**base, "external_id": "club", "kind": "club", "home": "C", "away": "D"},
            {**base, "external_id": "not-played", "state": "pre", "home_goals": None, "away_goals": None, "home": "E", "away": "F"},
        ]
        results = intl.espn_results(pd.DataFrame(rows))
        self.assertEqual(list(results["external_id"]), ["ok"])
        self.assertEqual(results.iloc[0]["home_team"], "Czech Republic")  # ESPN's spelling is translated to the dataset's


class ClubNameMatchingTest(unittest.TestCase):
    def test_common_abbreviations_resolve(self):
        known = {"Man United", "Man City", "Wolves", "Nott'm Forest", "Brighton", "Leeds"}
        self.assertEqual(match_club("Manchester United", known), "Man United")
        self.assertEqual(match_club("Manchester City", known), "Man City")
        self.assertEqual(match_club("Brighton & Hove Albion", known), "Brighton")
        self.assertEqual(match_club("Leeds United", known), "Leeds")
        self.assertEqual(match_club("Nottingham Forest", known), "Nott'm Forest")  # via the alias table
        self.assertEqual(match_club("Wolverhampton Wanderers", known), "Wolves")

    def test_short_prefixes_never_match_a_different_club(self):
        # 'St' must not be treated as a prefix of 'Stuttgart'/'Standard', nor 'Le' of 'Lens'
        self.assertIsNone(match_club("VfB Stuttgart", {"St Pauli"}))
        self.assertIsNone(match_club("Standard Liege", {"St. Gilloise"}))
        self.assertIsNone(match_club("Le Havre AC", {"Lens"}))
        self.assertEqual(match_club("VfB Stuttgart", {"St Pauli", "Stuttgart"}), "Stuttgart")

    def test_an_unknown_or_ambiguous_club_resolves_to_nothing_rather_than_a_guess(self):
        self.assertIsNone(match_club("Brand New FC", {"Man United", "Leeds"}))
        self.assertIsNone(match_club("Real Valladolid", {"Real Madrid", "Real Betis"}))

    def test_current_season_names_win_over_older_spellings(self):
        resolved, unresolved = match_clubs({"FC Cologne"}, {"FC Koln"}, {"FC Koln", "Koln"})
        self.assertEqual((resolved, unresolved), ({"FC Cologne": "FC Koln"}, []))


if __name__ == "__main__":
    unittest.main()
