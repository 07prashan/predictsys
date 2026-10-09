"""NBA basketball: rating movement, the scoring model, and the shape of a prediction -
no draw, a priced total, and a likely scoreline. Offline - the ESPN feed is mocked.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import espn  # noqa: E402
import nba  # noqa: E402


def game(home, away, home_points, away_points, when, external_id):
    return {
        "external_id": external_id,
        "home": home,
        "away": away,
        "home_points": home_points,
        "away_points": away_points,
        "kickoff_utc": pd.Timestamp(when, tz="UTC"),
        "state": "post",
    }


def history_frame() -> pd.DataFrame:
    rows = [
        game("Celtics", "Knicks", 120, 100, "2026-01-05", "espn:1"),
        game("Knicks", "Celtics", 95, 110, "2026-01-08", "espn:2"),
        game("Celtics", "Heat", 130, 118, "2026-01-10", "espn:3"),
        game("Heat", "Knicks", 105, 108, "2026-01-12", "espn:4"),
    ]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["kickoff_utc"]).dt.tz_convert(None).dt.normalize()
    df["winner"] = (df["home_points"] > df["away_points"]).map({True: "H", False: "A"})
    return df


def fixtures_frame() -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC")
    return pd.DataFrame(
        [
            {
                "external_id": "espn:future",
                "kickoff_utc": now + pd.Timedelta(days=1),
                "state": "pre",
                "status": "STATUS_SCHEDULED",
                "season_type": "preseason",
                "competition": "NBA Preseason",
                "home": "Celtics",
                "away": "Knicks",
                "home_logo": "bos.png",
                "away_logo": "nyk.png",
                "home_points": None,
                "away_points": None,
            }
        ]
    )


class SeasonYearTest(unittest.TestCase):
    def test_october_is_the_start_of_next_years_season(self):
        self.assertEqual(nba.season_year(dt.date(2026, 10, 9)), 2027)

    def test_january_is_the_back_half_of_the_current_season(self):
        self.assertEqual(nba.season_year(dt.date(2026, 2, 9)), 2026)


class RatingsTest(unittest.TestCase):
    def test_even_teams_at_home_win_more_than_half_the_time(self):
        p = nba.win_probability(1500.0, 1500.0)
        self.assertGreater(p, 0.5)
        self.assertAlmostEqual(p, 1.0 / (1.0 + 10 ** (-nba.HOME_ADVANTAGE / 400.0)), places=6)

    def test_beating_a_team_twice_moves_the_rating_the_right_way(self):
        ratings = nba.rate_matches(history_frame())
        self.assertGreater(ratings.rating("Celtics"), nba.INITIAL_RATING)
        self.assertLess(ratings.rating("Heat"), nba.INITIAL_RATING)
        self.assertEqual(list(ratings.form["Celtics"])[-1], "W")

    def test_probabilities_are_complementary(self):
        p = nba.win_probability(1600.0, 1400.0)
        self.assertAlmostEqual(p + (1 - p), 1.0, places=9)
        self.assertGreater(p, 0.5)


class ScoringModelTest(unittest.TestCase):
    def test_a_free_scoring_team_gets_a_higher_total(self):
        history = history_frame()
        offence, defence, league_avg = nba.scoring_model(history, pd.Timestamp("2026-01-13"))
        self.assertGreater(offence["Celtics"], offence["Heat"])
        self.assertGreater(league_avg, 0)

    def test_predictions_have_a_likely_score_and_a_total_line(self):
        history = history_frame()
        ratings = nba.rate_matches(history)
        model = nba.scoring_model(history, pd.Timestamp("2026-01-13"))
        preds = nba.predict_fixtures(fixtures_frame(), ratings, model)
        row = preds.iloc[0]
        self.assertEqual((row["sport"], row["league"], row["competition"]), ("basketball", "NBA", "NBA Preseason"))
        self.assertEqual(row["prob_draw"], 0.0)
        self.assertAlmostEqual(row["prob_home"] + row["prob_away"], 1.0, places=9)
        self.assertIn(row["predicted_outcome"], ("H", "A"))
        # predicted points are whole numbers (the DB stores them as INTEGER)
        self.assertEqual(row["correct_score_home"], round(row["correct_score_home"]))
        self.assertEqual(row["correct_score_away"], round(row["correct_score_away"]))
        # the over/under line is a half-point around the expected total, and the two
        # probabilities of the pick's own market sum to one
        self.assertEqual(row["total_line"] * 2, round(row["total_line"] * 2))
        self.assertAlmostEqual(row["over_prob"] + row["under_prob"], 1.0, places=9)
        self.assertGreaterEqual(row["best_pick_prob"], 0.5)


class BuildTest(unittest.TestCase):
    def test_build_is_offline_clean_with_a_mocked_feed(self):
        finished = game("Celtics", "Knicks", 120, 100, "2026-01-05", "espn:1")
        scoreboard = pd.DataFrame([finished, fixtures_frame().iloc[0].to_dict()])[espn.NBA_COLUMNS]

        with mock.patch.object(espn, "nba_team_ids", return_value=[1, 2]), \
             mock.patch.object(espn, "nba_team_games", return_value=[finished]), \
             mock.patch.object(espn, "fetch_nba", return_value=scoreboard):
            out = nba.build(dt.date(2026, 1, 6), horizon_days=7)

        self.assertEqual(list(out["predictions"]["external_id"]), ["espn:future"])
        self.assertEqual(list(out["results"]["external_id"]), ["espn:1"])
        self.assertEqual(out["results"].iloc[0]["outcome"], "H")
        self.assertEqual(out["ratings"].iloc[0]["league"], "NBA")
        self.assertEqual(set(out["matches"]["league"]), {"NBA"})

    def test_build_with_an_empty_feed_returns_empty_frames_not_an_error(self):
        with mock.patch.object(espn, "nba_team_ids", return_value=[]), \
             mock.patch.object(espn, "fetch_nba", return_value=pd.DataFrame(columns=espn.NBA_COLUMNS)):
            out = nba.build(dt.date(2026, 1, 6), horizon_days=7)
        self.assertTrue(out["predictions"].empty)
        self.assertTrue(out["matches"].empty)


if __name__ == "__main__":
    unittest.main()
