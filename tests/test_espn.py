"""How ESPN's feed is read: what counts as a match we can predict, and what must be dropped.
Uses small hand-built events in ESPN's shape - offline.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import espn  # noqa: E402


def soccer_event(state="pre", status="STATUS_SCHEDULED", league_id=2395, home_score="2", away_score="1", **over) -> dict:
    event = {
        "id": "401", "uid": f"s:600~l:{league_id}~e:401", "date": "2026-10-03T18:45Z",
        "competitions": [
            {
                "status": {"type": {"state": state, "name": status}},
                "competitors": [
                    {"homeAway": "home", "score": home_score, "form": "WWDLW", "team": {"displayName": "Spain", "logo": "es.png"}},
                    {"homeAway": "away", "score": away_score, "form": "LDLLL", "team": {"displayName": "Czechia", "logo": "cz.png"}},
                ],
            }
        ],
    }
    event.update(over)
    return event


def tennis_comp(round_name="Round 2", state="pre", names=("Carlos Alcaraz", "Matteo Arnaldi"), winner=None, uid="s:850~l:851~e:5-2026~c:1") -> dict:
    def side(order, name):
        athlete = None if name is None else {"displayName": name, "flag": {"href": f"{order}.png"}}
        won = winner == order
        return {"order": order, "athlete": athlete, "winner": won, "linescores": [{"winner": won}, {"winner": won}] if state == "post" else []}

    return {
        "uid": uid, "startDate": "2026-10-03T03:00Z", "round": {"displayName": round_name},
        "status": {"type": {"state": state, "name": "STATUS_FINAL" if state == "post" else "STATUS_SCHEDULED"}},
        "competitors": [side(1, names[0]), side(2, names[1])], "notes": [{"text": "result"}],
    }


def nba_event(state="pre", status="STATUS_SCHEDULED", slug="preseason", home_score=None, away_score=None) -> dict:
    def side(home_away, name, logo, score):
        return {
            "homeAway": home_away,
            "team": {"displayName": name, "logos": [{"href": logo}]},
            "score": None if score is None else {"value": score},
        }

    return {
        "id": "401", "date": "2026-10-10T00:00Z", "season": {"slug": slug},
        "competitions": [
            {
                "status": {"type": {"state": state, "name": status}},
                "competitors": [
                    side("home", "Dallas Mavericks", "dal.png", home_score),
                    side("away", "Houston Rockets", "hou.png", away_score),
                ],
            }
        ],
    }



class SoccerParseTest(unittest.TestCase):
    def test_a_tracked_league_event_is_parsed(self):
        record = espn._parse_soccer_event(soccer_event())
        self.assertEqual((record["kind"], record["league_code"], record["competition"]), ("intl", "INT", "UEFA Nations League"))
        self.assertEqual((record["home"], record["away"], record["state"]), ("Spain", "Czechia", "pre"))
        self.assertEqual(str(record["kickoff_utc"]), "2026-10-03 18:45:00+00:00")
        self.assertIsNone(record["home_goals"])  # no score exists before the match

    def test_club_leagues_map_to_football_data_codes(self):
        record = espn._parse_soccer_event(soccer_event(league_id=700))
        self.assertEqual((record["kind"], record["league_code"]), ("club", "E0"))

    def test_only_finished_matches_carry_a_score(self):
        record = espn._parse_soccer_event(soccer_event(state="post", status="STATUS_FULL_TIME"))
        self.assertEqual((record["home_goals"], record["away_goals"]), (2, 1))

    def test_untracked_leagues_are_ignored(self):
        self.assertIsNone(espn._parse_soccer_event(soccer_event(league_id=12345)))  # e.g. a college or youth competition
        self.assertIsNone(espn._parse_soccer_event({**soccer_event(), "uid": "no-league-id-here"}))


class TennisParseTest(unittest.TestCase):
    event = {"name": "China Open", "major": False}

    def parse(self, comp, tour="ATP"):
        return espn._parse_tennis_match(comp, self.event, tour)

    def test_a_scheduled_main_draw_match_is_parsed(self):
        record = self.parse(tennis_comp())
        self.assertEqual((record["p1"], record["p2"], record["state"], record["round"]), ("Carlos Alcaraz", "Matteo Arnaldi", "pre", "Round 2"))
        self.assertEqual(record["winner"], 0)

    def test_qualifying_rounds_are_not_main_draw(self):
        self.assertIsNone(self.parse(tennis_comp(round_name="Qualifying 1st Round")))

    def test_draw_slots_still_waiting_on_an_earlier_round_are_skipped(self):
        for placeholder in (None, "TBD", "Winner of Match 12", "Bye"):
            self.assertIsNone(self.parse(tennis_comp(names=("Carlos Alcaraz", placeholder))), placeholder)

    def test_a_finished_match_records_winner_and_sets(self):
        record = self.parse(tennis_comp(state="post", winner=2))
        self.assertEqual((record["winner"], record["p1_sets"], record["p2_sets"]), (2, 0, 2))

    def test_only_this_tours_own_singles_draw_is_read_from_a_joint_event(self):
        # a joint ATP+WTA event appears in both feeds with both draws inside it
        joint = {
            "name": "China Open", "major": False, "status": {"type": {"description": "Scheduled"}},
            "groupings": [
                {"grouping": {"slug": "mens-singles"}, "competitions": [tennis_comp(uid="men")]},
                {"grouping": {"slug": "womens-singles"}, "competitions": [tennis_comp(uid="women", names=("Iga Swiatek", "Coco Gauff"))]},
                {"grouping": {"slug": "mens-doubles"}, "competitions": [tennis_comp(uid="doubles")]},
            ],
        }
        from unittest import mock

        with mock.patch.object(espn, "get_json", return_value={"events": [joint]}):
            atp = espn._tennis_records("ATP", None)
            wta = espn._tennis_records("WTA", None)
        self.assertEqual([r["external_id"] for r in atp], ["espn:men"])
        self.assertEqual([r["external_id"] for r in wta], ["espn:women"])

    def test_canceled_events_are_skipped(self):
        from unittest import mock

        canceled = {
            "name": "Samsun Open", "status": {"type": {"description": "Canceled"}},
            "groupings": [{"grouping": {"slug": "womens-singles"}, "competitions": [tennis_comp()]}],
        }
        with mock.patch.object(espn, "get_json", return_value={"events": [canceled]}):
            self.assertEqual(espn._tennis_records("WTA", None), [])


class NbaParseTest(unittest.TestCase):
    def test_preseason_is_named_and_has_no_score_before_tipoff(self):
        record = espn._parse_nba_event(nba_event())
        self.assertEqual(
            (record["competition"], record["state"], record["home"], record["away"]),
            ("NBA Preseason", "pre", "Dallas Mavericks", "Houston Rockets"),
        )
        self.assertIsNone(record["home_points"])
        self.assertEqual(record["home_logo"], "dal.png")

    def test_regular_season_and_playoffs_get_their_own_names(self):
        self.assertEqual(espn._parse_nba_event(nba_event(slug="regular-season"))["competition"], "NBA")
        self.assertEqual(espn._parse_nba_event(nba_event(slug="post-season"))["competition"], "NBA Playoffs")

    def test_only_finished_games_carry_points(self):
        record = espn._parse_nba_event(nba_event(state="post", status="STATUS_FINAL", home_score=112, away_score=108))
        self.assertEqual((record["home_points"], record["away_points"]), (112, 108))
        self.assertEqual(record["external_id"], "espn:401")


if __name__ == "__main__":
    unittest.main()
