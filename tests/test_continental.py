"""The global cross-border model: one worldwide club scale calibrated by continental
results, and predictions for the continental competitions. Offline - hand-built frames,
no network, no clock beyond the rows' own kickoffs.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import continental  # noqa: E402
import espn  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def domestic_row(date, home, away, hg, ag, league="E0"):
    ftr = "H" if hg > ag else "A" if hg < ag else "D"
    return {
        "league": league, "Date": pd.Timestamp(date), "HomeTeam": home, "AwayTeam": away,
        "FTHG": hg, "FTAG": ag, "FTR": ftr, "season": pd.Timestamp(date).year,
    }


def make_raw(n: int = 40) -> dict:
    """Two synthetic leagues with a clear pecking order and some noise, so the Elo pass and
    the goal regression have something real to chew on."""
    rng = np.random.RandomState(7)
    frames = {}
    for code, names in {"E0": [f"Alpha {i}" for i in range(6)], "SP1": [f"Beta {i}" for i in range(6)]}.items():
        rows = []
        for i in range(n):
            home, away = rng.choice(names, 2, replace=False)
            # stronger (lower index) side wins more often - a real ordering to recover
            edge = (names.index(away) - names.index(home)) * 0.12
            p_home = 0.4 + edge
            u = rng.rand()
            hg, ag = (2, 1) if u < p_home else (1, 1) if u < p_home + 0.22 else (0, 1)
            date = pd.Timestamp("2025-01-05") + pd.Timedelta(days=i * 9)
            rows.append(domestic_row(date, home, away, hg, ag, code))
        frames[code] = pd.DataFrame(rows)
    return frames


def continental_row(eid, day, home, away, hg, ag, code="UCL"):
    """A finished continental tie in espn's frame, mixing covered clubs (Alpha/Beta, which
    also play domestically) with one from an uncovered league (Al Hilal)."""
    return {
        "external_id": f"espn:{eid}", "kickoff_utc": pd.Timestamp(day, tz="UTC"), "kind": "continental",
        "league_id": None, "league_code": code, "competition": None, "state": "post",
        "status": "STATUS_FULL_TIME", "home": home, "away": away, "home_logo": None, "away_logo": None,
        "home_form": None, "away_form": None, "home_goals": hg, "away_goals": ag,
    }


def upcoming_row(eid, when, home, away, code="UCL"):
    return {
        "external_id": f"espn:{eid}", "kickoff_utc": pd.Timestamp(when, tz="UTC"), "kind": "continental",
        "league_id": None, "league_code": code, "competition": None, "state": "pre",
        "status": "STATUS_SCHEDULED", "home": home, "away": away, "home_logo": f"{home}.png",
        "away_logo": f"{away}.png", "home_form": None, "away_form": None, "home_goals": None, "away_goals": None,
    }


def soccer(extra: list) -> pd.DataFrame:
    if not extra:
        return pd.DataFrame(columns=espn.SOCCER_COLUMNS)
    frame = pd.DataFrame([{c: r.get(c) for c in espn.SOCCER_COLUMNS} for r in extra])
    frame["kickoff_utc"] = pd.to_datetime(frame["kickoff_utc"], utc=True)
    for col in ("home_goals", "away_goals"):
        frame[col] = frame[col].astype("Int64")
    return frame[espn.SOCCER_COLUMNS]


FINISHED = [
    continental_row(1, "2026-02-10 20:00", "Alpha 0", "Al Hilal", 2, 0),
    continental_row(2, "2026-02-10 20:00", "Beta 0", "Alpha 1", 1, 1),
    continental_row(3, "2026-03-04 20:00", "Al Hilal", "Beta 0", 3, 1),
    continental_row(4, "2026-04-01 20:00", "Alpha 1", "Alpha 0", 0, 1),
]


class RatingPassTest(unittest.TestCase):
    def rated(self, matches):
        return continental.rate_matches(continental.domestic_matches({"E0": pd.DataFrame(matches)}))

    def test_ratings_are_zero_sum(self):
        _, ratings, _ = self.rated([domestic_row("2026-01-01", "Strong", "Weak", 2, 0)])
        self.assertAlmostEqual(ratings["Strong"] + ratings["Weak"], 2 * continental.INITIAL_RATING, places=6)
        self.assertGreater(ratings["Strong"], continental.INITIAL_RATING)

    def test_a_win_moves_ratings_up_for_the_winner_and_down_for_the_loser(self):
        _, after_win, _ = self.rated([
            domestic_row("2026-01-01", "Strong", "Weak", 2, 0),
            domestic_row("2026-01-08", "Strong", "Weak", 1, 0),
        ])
        _, upset, _ = self.rated([
            domestic_row("2026-01-01", "Strong", "Weak", 2, 0),
            domestic_row("2026-01-08", "Strong", "Weak", 0, 1),
        ])
        # two comfortable home wins push the favourite further than a win then an upset
        self.assertGreater(after_win["Strong"] - continental.INITIAL_RATING, upset["Strong"] - continental.INITIAL_RATING)

    def test_a_continental_match_moves_ratings_further_than_a_domestic_one(self):
        # Same scoreline, so the only differences are the K and the (smaller) continental
        # home edge - which alone shifts the ratio a little off the plain 2.0.
        dom = continental.domestic_matches({"E0": pd.DataFrame([domestic_row("2026-01-01", "A", "B", 1, 0)])})
        _, dom_ratings, _ = continental.rate_matches(dom)
        cont = pd.DataFrame([{
            "date": pd.Timestamp("2026-01-01"), "home": "A", "away": "B", "home_goals": 1, "away_goals": 0,
            "kind": "continental", "competition": "UCL",
        }])
        _, cont_ratings, _ = continental.rate_matches(cont)
        dom_move = dom_ratings["A"] - continental.INITIAL_RATING
        cont_move = cont_ratings["A"] - continental.INITIAL_RATING
        self.assertGreater(cont_move, dom_move)
        self.assertAlmostEqual(
            cont_move / dom_move, continental.K_CONTINENTAL / continental.K_DOMESTIC, delta=0.2
        )

    def test_form_is_the_last_five_oldest_first(self):
        rows = [domestic_row(pd.Timestamp("2026-01-01") + pd.Timedelta(days=i * 7), "A", "B", hg, ag)
                for i, (hg, ag) in enumerate([(2, 0), (0, 1), (1, 1), (3, 0), (0, 2), (1, 0)])]
        _, _, form = self.rated(rows)
        self.assertEqual(form["A"], "LDWLW")  # the first win has aged out of the last five


class GoalsModelTest(unittest.TestCase):
    def test_equal_strengths_produce_equal_expected_goals(self):
        lam_h, lam_a = continental.expected_goals(np.array([1.2, 0.0, 0.1]), 1550.0, 1550.0)
        self.assertAlmostEqual(float(lam_h), float(lam_a), places=9)
        self.assertGreater(float(lam_h), 0)

    def test_the_stronger_side_is_expected_to_score_more(self):
        lam_h, lam_a = continental.expected_goals(np.array([1.2, 0.5, 0.1]), 1800.0, 1500.0)
        self.assertGreater(float(lam_h), float(lam_a))

    def test_a_bigger_gap_means_more_goals_for_the_favourite(self):
        coef = np.array([0.1, 0.5, 0.0])
        small, _ = continental.expected_goals(coef, 1600.0, 1500.0)
        big, _ = continental.expected_goals(coef, 1900.0, 1500.0)
        self.assertGreater(float(big), float(small))


class ResolutionTest(unittest.TestCase):
    def test_a_known_club_resolves_to_the_football_data_spelling(self):
        resolved, unresolved = continental.resolve_names({"Paris Saint-Germain"}, {"Paris SG", "Man City"})
        self.assertEqual(resolved["Paris Saint-Germain"], "Paris SG")
        self.assertEqual(unresolved, [])

    def test_a_club_from_an_uncovered_league_keeps_its_own_spelling(self):
        # rated off its continental results alone rather than dropped
        resolved, unresolved = continental.resolve_names({"Al Hilal"}, {"Man City"})
        self.assertEqual(resolved["Al Hilal"], "Al Hilal")
        self.assertEqual(unresolved, ["Al Hilal"])

    def test_big_clubs_whose_espn_name_out_tokenizes_the_domestic_one_still_resolve(self):
        known = {"Porto", "Inter", "AC Milan", "Nijmegen", "Guimaraes", "Club America", "America MG",
                 "Sao Paulo", "Talleres Cordoba", "Cordoba", "Inter Miami", "Internacional",
                 "West Ham", "Hamilton", "Shanghai Port"}
        expected = {
            "FC Porto": "Porto", "Internazionale": "Inter", "Inter Milan": "Inter",
            "NEC Nijmegen": "Nijmegen", "Vitória de Guimaraes": "Guimaraes",
            "América": "Club America", "São Paulo": "Sao Paulo",
            "Talleres (Córdoba)": "Talleres Cordoba", "Inter Miami CF": "Inter Miami",
            "West Ham United": "West Ham", "Shanghai Port FC": "Shanghai Port",
        }
        resolved, unresolved = continental.resolve_names(set(expected), known)
        self.assertEqual({name: resolved[name] for name in expected}, expected)
        self.assertEqual(unresolved, [])

    def test_an_ambiguous_name_is_never_guessed(self):
        resolved, unresolved = continental.resolve_names({"Sporting"}, {"Sporting CP", "Sporting Cristal"})
        self.assertEqual(resolved["Sporting"], "Sporting")  # neither candidate, until a human decides
        self.assertEqual(unresolved, ["Sporting"])


def fake_history(code, slug, year, final=None):
    """The backfilled history, as if the UCL years had been fetched - every other
    competition came back empty, which is also exactly what an off-season looks like."""
    return soccer(FINISHED) if code == "UCL" else pd.DataFrame(columns=espn.SOCCER_COLUMNS)


class BuildTest(unittest.TestCase):
    def build(self, daily_extra=()):
        daily = soccer(list(daily_extra) + [upcoming_row(9, "2026-10-12 20:00", "Alpha 0", "Al Hilal")])
        with mock.patch.object(espn, "fetch_league_history", side_effect=fake_history):
            return continental.build(make_raw(), daily, today=NOW.date(), horizon_days=10, now=pd.Timestamp(NOW))

    def test_upcoming_continental_fixtures_are_predicted(self):
        preds = self.build(daily_extra=[upcoming_row(10, "2026-10-13 20:00", "Beta 0", "Al Nassr")])["predictions"]
        self.assertEqual(len(preds), 2)
        row = preds[preds["away_team"] == "Al Hilal"].iloc[0]
        self.assertEqual((row["league"], row["home_team"], row["away_team"]), ("UCL", "Alpha 0", "Al Hilal"))
        self.assertAlmostEqual(row["prob_home"] + row["prob_draw"] + row["prob_away"], 1.0, places=6)
        for col in ("expected_goals_home", "btts_yes_prob", "over_2_5_prob", "best_pick_market", "kickoff_utc", "external_id"):
            self.assertIn(col, preds.columns)
        self.assertEqual(row["external_id"], "espn:9")
        self.assertEqual(row["home_logo"], "Alpha 0.png")
        # form is the global pool's own record - both of these clubs have continental results
        self.assertTrue(row["home_form"])
        self.assertTrue(row["away_form"])
        # a club that has never appeared in the pool shows no form rather than a guess
        debutant = preds[preds["away_team"] == "Al Nassr"].iloc[0]
        self.assertEqual(debutant["away_form"], "")

    def test_the_covered_club_is_the_favourite_on_the_global_scale(self):
        row = self.build()["predictions"].iloc[0]
        self.assertEqual(row["predicted_outcome"], "H")
        self.assertGreater(row["home_rating"], row["away_rating"])

    def test_history_and_ratings_reach_the_site_in_their_own_shape(self):
        out = self.build()
        self.assertEqual(set(out["matches"]["league"]), {"UCL"})
        self.assertIn("Al Hilal", set(out["matches"]["home_team"]) | set(out["matches"]["away_team"]))
        ratings = out["ratings"].set_index("team")
        self.assertEqual(set(out["ratings"]["league"]), {"UCL"})
        self.assertIn("Al Hilal", ratings.index)
        self.assertIn("Alpha 0", ratings.index)
        # the covered club's rating carries its domestic results; the uncovered one starts from scratch
        self.assertGreater(ratings.loc["Alpha 0", "elo_rating"], ratings.loc["Al Hilal", "elo_rating"])

    def test_a_team_seen_in_both_pools_gets_one_rating_not_two(self):
        ratings = self.build()["ratings"].set_index("team")
        # Alpha 0 played domestically AND in the continental history - the global scale must
        # have moved on its domestic results, not started over at the initial rating
        self.assertNotEqual(ratings.loc["Alpha 0", "elo_rating"], continental.INITIAL_RATING)

    def test_no_continental_fixtures_still_exports_history(self):
        daily = soccer([])
        with mock.patch.object(espn, "fetch_league_history", side_effect=fake_history):
            out = continental.build(make_raw(), daily, today=NOW.date(), now=pd.Timestamp(NOW))
        self.assertEqual(len(out["predictions"]), 0)
        self.assertGreater(len(out["matches"]), 0)


if __name__ == "__main__":
    unittest.main()
