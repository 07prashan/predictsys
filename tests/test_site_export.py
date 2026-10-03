"""The static files the website is built from: what's in them, what's left out (finished
matches), and that every number the page used to get from the API is still right.
Offline - runs on a throwaway database and a throwaway output folder.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import site_export  # noqa: E402
import storage  # noqa: E402
from tests.test_storage import football_row, tennis_row  # noqa: E402

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)


def at(hours_from_now: float) -> pd.Timestamp:
    return pd.Timestamp(NOW + dt.timedelta(hours=hours_from_now))


def history_rows(league: str, results: list) -> pd.DataFrame:
    """results: (date, winner/home, loser/away, home_goals, away_goals, result)"""
    return pd.DataFrame(
        [
            {"league": league, "date": d, "home_team": h, "away_team": a, "home_goals": hg, "away_goals": ag, "result": r}
            for d, h, a, hg, ag, r in results
        ]
    )


class SiteExportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(storage, "DB_PATH", Path(tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = storage.connect()
        self.addCleanup(self.conn.close)
        self.out = Path(tmp.name) / "data"

        save = lambda *rows: storage.save_predictions(self.conn, pd.DataFrame(list(rows)), predicted_at="2026-10-01T00:00:00")
        save(
            football_row(external_id="e:upcoming", home_team="Spain", away_team="Czech Republic", kickoff_utc=at(6)),
            football_row(external_id="e:over", home_team="Italy", away_team="Turkey", kickoff_utc=at(-7)),  # kicked off 7h ago
            football_row(external_id="e:graded", home_team="France", away_team="Belgium", kickoff_utc=at(-30)),
            tennis_row(external_id="t:upcoming", home_team="Carlos Alcaraz", away_team="Jannik Sinner", kickoff_utc=at(3)),
            football_row(external_id=None, kickoff_utc=None, home_team="Legacy", away_team="Club", league="E0", match_date="2026-10-01"),
        )
        storage.settle_by_external_id(
            self.conn,
            pd.DataFrame([{"external_id": "e:graded", "outcome": "H", "home_score": 2, "away_score": 0}]),
        )
        storage.save_matches(
            self.conn,
            pd.concat(
                [
                    history_rows("INT", [("2026-09-01", "Spain", "Czech Republic", 3, 0, "H"), ("2025-06-01", "Czech Republic", "Spain", 1, 1, "D"),
                                         ("2024-03-01", "Czech Republic", "Spain", 2, 0, "H")]),
                    history_rows("ATP", [("2026-04-11", "Jannik Sinner", "Carlos Alcaraz", 2, 0, "H"), ("2026-01-20", "Carlos Alcaraz", "Jannik Sinner", 3, 2, "H")]),
                ]
            ),
        )
        storage.save_team_ratings(
            self.conn,
            pd.DataFrame(
                [
                    {"league": "INT", "team": "Spain", "elo_rating": 2300.0},
                    {"league": "INT", "team": "Czech Republic", "elo_rating": 1700.0},
                    {"league": "ATP", "team": "Carlos Alcaraz", "elo_rating": 2200.0},
                    {"league": "ATP", "team": "Jannik Sinner", "elo_rating": 2250.0},
                ]
            ),
        )
        self.stats = site_export.export_site(self.conn, self.out, now=NOW)

    def load(self, *parts):
        return json.loads(self.out.joinpath(*parts).read_text(encoding="utf-8"))

    def test_upcoming_lists_only_matches_still_to_be_played(self):
        data = self.load("predictions.json")
        names = {(r["home_team"], r["away_team"]) for r in data["rows"]}
        self.assertEqual(data["generated_at"], "2026-10-03T12:00:00Z")
        self.assertIn(("Spain", "Czech Republic"), names)
        self.assertIn(("Carlos Alcaraz", "Jannik Sinner"), names)
        self.assertNotIn(("Italy", "Turkey"), names)  # over - never shown again
        self.assertNotIn(("France", "Belgium"), names)  # graded
        self.assertNotIn(("Legacy", "Club"), names)  # an old date-only row whose date has passed
        self.assertEqual([r["kickoff_utc"] for r in data["rows"]], sorted(r["kickoff_utc"] for r in data["rows"]))  # soonest first

    def test_each_row_carries_what_the_page_needs(self):
        spain = next(r for r in self.load("predictions.json")["rows"] if r["home_team"] == "Spain")
        self.assertEqual((spain["sport"], spain["competition"], spain["country"], spain["league_name"]), ("football", "UEFA Nations League", "International", "UEFA Nations League"))
        self.assertEqual(spain["home_form"], "WWWWW")  # supplementary markets are flattened in from markets_json
        self.assertEqual(spain["home_logo"], "https://example.test/esp.png")
        self.assertNotIn("markets_json", spain)
        self.assertNotIn("expected_goals_home", spain)  # unused by the page - not shipped

        tennis = next(r for r in self.load("predictions.json")["rows"] if r["sport"] == "tennis")
        self.assertEqual((tennis["country"], tennis["surface"], tennis["grand_slam"] if "grand_slam" in tennis else None), ("ATP Tour", "Hard", 1))

    def test_floats_are_trimmed_to_four_decimals(self):
        row = self.load("predictions.json")["rows"][0]
        self.assertTrue(all(len(str(v).split(".")[-1]) <= 4 for v in row.values() if isinstance(v, float)))

    def test_history_and_scoreboard_cover_graded_matches(self):
        history = self.load("history.json")
        self.assertEqual([(r["home_team"], r["actual_outcome"], r["actual_home_goals"]) for r in history], [("France", "H", 2)])
        board = {r["league"]: r for r in self.load("scoreboard.json")}
        self.assertEqual(board["INT"]["n_settled"], 1)
        self.assertEqual(board["ALL"]["accuracy"], 1.0)
        self.assertAlmostEqual(board["ALL"]["log_loss"], -__import__("math").log(0.9), places=6)

    def test_team_profile(self):
        spain = self.load("teams", "INT.json")["Spain"]
        self.assertEqual((spain["league_rank"], spain["league_size"], spain["elo_rating"]), (1, 2, 2300.0))
        self.assertEqual(spain["recent_matches"][0]["outcome"], "W")  # newest first: the 3-0 win
        self.assertEqual([m["outcome"] for m in spain["recent_matches"]], ["W", "D", "L"])
        self.assertAlmostEqual(spain["recent_form_ppg"], (3 + 1 + 0) / 3, places=3)
        self.assertEqual(spain["next_fixture"]["away_team"], "Czech Republic")  # from the upcoming list
        self.assertEqual(self.load("teams", "ATP.json")["Carlos Alcaraz"]["sport"], "tennis")

    def test_tennis_profile_counts_wins_for_the_winner_side(self):
        alcaraz = self.load("teams", "ATP.json")["Carlos Alcaraz"]
        self.assertEqual([m["outcome"] for m in alcaraz["recent_matches"]], ["L", "W"])  # lost in April, won in January
        self.assertEqual(alcaraz["recent_win_rate"], 0.5)

    def test_head_to_head_is_oriented_to_the_requested_home_side(self):
        spain = self.load("h2h", "INT.json")["Spain|Czech Republic"]
        self.assertEqual(spain["summary"], {"home_wins": 1, "away_wins": 1, "draws": 1})
        self.assertEqual(spain["total"], 3)
        self.assertEqual(spain["meetings"][0]["date"], "2026-09-01")  # newest first

        # tennis: the winner is stored on the "home" side, and the pair is asked about the other way round
        tennis = self.load("h2h", "ATP.json")["Carlos Alcaraz|Jannik Sinner"]
        self.assertEqual(tennis["summary"], {"home_wins": 1, "away_wins": 1, "draws": 0})

    def test_nothing_half_written_is_left_behind(self):
        self.assertEqual(list(self.out.rglob("*.tmp")), [])
        self.assertEqual(self.stats["upcoming"], 2)
        self.assertEqual(self.stats["history"], 1)

    def test_groupings_used_by_the_competition_view(self):
        self.assertEqual(site_export.group_of("E0"), "England")
        self.assertEqual(site_export.group_of("INT"), "International")
        self.assertEqual((site_export.group_of("ATP"), site_export.group_of("WTA")), ("ATP Tour", "WTA Tour"))
        self.assertEqual(site_export.competition_of("SP1"), "La Liga")

    def test_an_empty_database_still_produces_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(storage, "DB_PATH", Path(tmp) / "empty.db"):
            conn = storage.connect()
            out = Path(tmp) / "data"
            site_export.export_site(conn, out, now=NOW)
            conn.close()
            self.assertEqual(json.loads((out / "predictions.json").read_text(encoding="utf-8"))["rows"], [])
            self.assertEqual(json.loads((out / "scoreboard.json").read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
