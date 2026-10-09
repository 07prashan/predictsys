"""Storage behaviour that must never regress: predictions are insert-once, only the
schedule of an already-predicted match may change, results settle by ESPN id, and a
partial export never wipes another league's history. Offline - runs on a throwaway DB.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import storage  # noqa: E402


def football_row(**overrides) -> dict:
    row = {
        "league": "INT", "sport": "football", "competition": "UEFA Nations League",
        "match_date": "2026-10-03", "kickoff_utc": pd.Timestamp("2026-10-03T18:45:00Z"),
        "home_team": "Spain", "away_team": "Czech Republic", "external_id": "espn:1",
        "prob_home": 0.90, "prob_draw": 0.07, "prob_away": 0.03, "predicted_outcome": "H",
        "expected_goals_home": 3.1, "expected_goals_away": 0.4, "correct_score_home": 3, "correct_score_away": 0,
        "btts_yes_prob": 0.2, "over_2_5_prob": 0.7, "over_1_5_prob": 0.9,
        "best_pick_market": "Result", "best_pick_label": "Home Win", "best_pick_prob": 0.9,
        "home_logo": "https://example.test/esp.png", "home_form": "WWWWW", "away_form": "LDLLL",
    }
    row.update(overrides)
    return row


def basketball_row(**overrides) -> dict:
    # basketball has no draw, and carries an over/under line and a predicted total instead
    row = {
        "league": "NBA", "sport": "basketball", "competition": "NBA Preseason", "round": "",
        "match_date": "2026-10-10", "kickoff_utc": pd.Timestamp("2026-10-10T00:00:00Z"),
        "home_team": "Dallas Mavericks", "away_team": "Houston Rockets", "external_id": "espn:999",
        "prob_home": 0.62, "prob_draw": 0.0, "prob_away": 0.38, "predicted_outcome": "H",
        "correct_score_home": 115, "correct_score_away": 110,
        "best_pick_market": "Moneyline", "best_pick_label": "Home Win", "best_pick_prob": 0.62,
        "home_logo": "dal.png", "away_logo": "hou.png", "home_form": "WWLWW", "away_form": "LLWLL",
        "total_line": 224.5, "over_prob": 0.55, "under_prob": 0.45, "predicted_total": 224.8, "predicted_spread": 5.2,
    }
    row.update(overrides)
    return row


def tennis_row(**overrides) -> dict:
    # no goal markets at all - tennis has none, and storage must accept that
    row = {
        "league": "ATP", "sport": "tennis", "competition": "China Open", "round": "Round 2",
        "match_date": "2026-10-03", "kickoff_utc": pd.Timestamp("2026-10-03T03:00:00Z"),
        "home_team": "Carlos Alcaraz", "away_team": "Matteo Arnaldi", "external_id": "espn:s:850~c:9",
        "prob_home": 0.92, "prob_draw": 0.0, "prob_away": 0.08, "predicted_outcome": "H",
        "correct_score_home": 2, "correct_score_away": 0,
        "best_pick_market": "Match Winner", "best_pick_label": "Carlos Alcaraz to win", "best_pick_prob": 0.92,
        "surface": "Hard", "best_of": 3, "grand_slam": 1, "straight_sets_prob": 0.7,
    }
    row.update(overrides)
    return row


class StorageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", Path(tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = storage.connect()
        self.addCleanup(self.conn.close)

    def save(self, *rows):
        return storage.save_predictions(self.conn, pd.DataFrame(list(rows)), predicted_at="2026-10-01T00:00:00")

    def fetch(self, external_id):
        self.conn.row_factory = None
        cols = [c[1] for c in self.conn.execute("PRAGMA table_info(predictions)")]
        values = self.conn.execute("SELECT * FROM predictions WHERE external_id = ?", (external_id,)).fetchone()
        return dict(zip(cols, values))

    def test_football_and_tennis_rows_both_store(self):
        self.assertEqual(self.save(football_row(), tennis_row()), 2)
        match, tennis = self.fetch("espn:1"), self.fetch("espn:s:850~c:9")
        self.assertEqual(match["kickoff_utc"], "2026-10-03T18:45:00Z")
        self.assertEqual(match["sport"], "football")
        self.assertIsNone(tennis["expected_goals_home"])  # a missing market is NULL, not a crash or a 0
        self.assertEqual(tennis["correct_score_home"], 2)
        self.assertIn('"surface": "Hard"', tennis["markets_json"])
        self.assertIn('"grand_slam": 1', tennis["markets_json"])


    def test_a_basketball_row_stores_with_its_total_markets(self):
        self.assertEqual(self.save(basketball_row()), 1)
        row = self.fetch("espn:999")
        self.assertEqual((row["sport"], row["prob_draw"], row["correct_score_home"]), ("basketball", 0.0, 115))
        self.assertIn('"total_line": 224.5', row["markets_json"])
        self.assertIn('"predicted_total": 224.8', row["markets_json"])

    def test_a_row_with_no_sport_defaults_to_football(self):
        row = football_row(external_id=None)
        del row["sport"]
        self.save(row)
        self.assertEqual(self.conn.execute("SELECT sport FROM predictions").fetchone()[0], "football")

    def test_same_match_is_never_duplicated_and_keeps_its_original_prediction(self):
        self.save(football_row())
        moved = football_row(
            match_date="2026-10-04", kickoff_utc=pd.Timestamp("2026-10-04T16:00:00Z"), prob_home=0.55,
        )
        self.assertEqual(self.save(moved), 0)  # not a new prediction...
        row = self.fetch("espn:1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 1)
        self.assertEqual(row["kickoff_utc"], "2026-10-04T16:00:00Z")  # ...but the schedule follows the match
        self.assertEqual(row["match_date"], "2026-10-04")
        self.assertEqual(row["prob_home"], 0.90)  # the prediction made before the match is the one that counts

    def test_fixture_first_stored_without_a_kickoff_time_gains_it_later(self):
        legacy = football_row(external_id=None, kickoff_utc=None, home_logo=None)
        self.save(legacy)
        self.assertEqual(self.save(football_row()), 0)  # same league/date/teams, now with a time + id + crest
        row = self.conn.execute("SELECT kickoff_utc, external_id, home_logo FROM predictions").fetchone()
        self.assertEqual(row, ("2026-10-03T18:45:00Z", "espn:1", "https://example.test/esp.png"))

    def test_settling_by_espn_id(self):
        self.save(football_row(), tennis_row(predicted_outcome="H"))
        results = pd.DataFrame(
            [
                {"external_id": "espn:1", "outcome": "H", "home_score": 3, "away_score": 0},
                {"external_id": "espn:s:850~c:9", "outcome": "A", "home_score": 0, "away_score": 2},
                {"external_id": "espn:never-predicted", "outcome": "D", "home_score": 1, "away_score": 1},
            ]
        )
        self.assertEqual(storage.settle_by_external_id(self.conn, results), 2)
        football, tennis = self.fetch("espn:1"), self.fetch("espn:s:850~c:9")
        self.assertEqual((football["actual_outcome"], football["correct"], football["actual_home_goals"]), ("H", 1, 3))
        self.assertEqual((tennis["actual_outcome"], tennis["correct"], tennis["actual_away_goals"]), ("A", 0, 2))
        # settled rows are final: a second pass (or a contradictory later result) changes nothing
        self.assertEqual(storage.settle_by_external_id(self.conn, results), 0)

    def test_settle_handles_an_empty_result_set(self):
        self.save(football_row())
        self.assertEqual(storage.settle_by_external_id(self.conn, pd.DataFrame()), 0)

    def test_exporting_one_leagues_history_keeps_the_others(self):
        def frame(league, n):
            return pd.DataFrame(
                {"league": league, "date": "2026-01-01", "home_team": "A", "away_team": "B",
                 "home_goals": 1, "away_goals": 0, "result": "H"}, index=range(n),
            )

        storage.save_matches(self.conn, frame("E0", 3))
        storage.save_matches(self.conn, frame("ATP", 2))
        storage.save_matches(self.conn, frame("ATP", 5))  # a re-export replaces, never appends
        counts = dict(self.conn.execute("SELECT league, COUNT(*) FROM matches GROUP BY league"))
        self.assertEqual(counts, {"E0": 3, "ATP": 5})


if __name__ == "__main__":
    unittest.main()
