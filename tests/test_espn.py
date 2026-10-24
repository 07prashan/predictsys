"""How ESPN's feed is read: what counts as a match we can predict, and what must be dropped.
Uses small hand-built events in ESPN's shape - offline.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_world_leagues_map_to_their_own_codes(self):
        self.assertEqual(espn._parse_soccer_event(soccer_event(league_id=630))["league_code"], "BRA")
        self.assertEqual(espn._parse_soccer_event(soccer_event(league_id=745))["league_code"], "ARG")
        self.assertEqual(espn._parse_soccer_event(soccer_event(league_id=770))["league_code"], "USA")

    def test_continental_competitions_get_their_own_kind(self):
        # UCL/UEL/Conference League/Libertadores/ACL Elite/... - the clubs come from every
        # league at once, so these are priced by the global model, not any league's
        self.assertEqual(
            espn._parse_soccer_event(soccer_event(league_id=775))["kind"], "continental"
        )
        record = espn._parse_soccer_event(soccer_event(league_id=3902))
        self.assertEqual((record["kind"], record["league_code"]), ("continental", "ACLE"))
        record = espn._parse_soccer_event(soccer_event(league_id=783, state="post", status="STATUS_FULL_TIME"))
        self.assertEqual((record["league_code"], record["home_goals"]), ("LIB", 2))

    def test_only_finished_matches_carry_a_score(self):
        record = espn._parse_soccer_event(soccer_event(state="post", status="STATUS_FULL_TIME"))
        self.assertEqual((record["home_goals"], record["away_goals"]), (2, 1))

    def test_untracked_leagues_are_ignored(self):
        self.assertIsNone(espn._parse_soccer_event(soccer_event(league_id=12345)))  # e.g. a college or youth competition
        self.assertIsNone(espn._parse_soccer_event({**soccer_event(), "uid": "no-league-id-here"}))


class LeagueHistoryTest(unittest.TestCase):
    """The competition-year fetch that backfills continental history - one request per
    competition-year, cached on disk so a scheduled run never refetches a finished season."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(espn, "CACHE_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_year_of_one_competition_comes_back_parsed(self):
        events = {"events": [
            soccer_event(state="post", status="STATUS_FULL_TIME", league_id=775, home_score="3", away_score="0"),
            {**soccer_event(league_id=775), "id": "402", "uid": "s:600~l:775~e:402"},
        ]}
        with mock.patch.object(espn, "get_json", return_value=events) as fetch:
            df = espn.fetch_league_history("UCL", "uefa.champions", 2025, final=True)
        fetch.assert_called_once()
        self.assertEqual((fetch.call_args[0][0]).split("/")[-2:], ["uefa.champions", "scoreboard"])
        self.assertEqual(len(df), 2)
        self.assertEqual(set(df["league_code"]), {"UCL"})
        self.assertEqual(df.iloc[0]["home_goals"], 3)
        self.assertTrue(df["kickoff_utc"].dt.tz)  # still tz-aware UTC, like every other feed

    def test_a_finished_year_is_never_refetched(self):
        events = {"events": [soccer_event(state="post", status="STATUS_FULL_TIME", league_id=775)]}
        with mock.patch.object(espn, "get_json", return_value=events) as fetch:
            espn.fetch_league_history("UCL", "uefa.champions", 2025, final=True)
            again = espn.fetch_league_history("UCL", "uefa.champions", 2025, final=True)
        self.assertEqual(fetch.call_count, 1)  # the second call was answered from disk
        self.assertEqual(len(again), 1)

    def test_the_current_year_is_always_refetched(self):
        events = {"events": [soccer_event(state="post", status="STATUS_FULL_TIME", league_id=775)]}
        this_year = dt.datetime.now(dt.timezone.utc).year
        with mock.patch.object(espn, "get_json", return_value=events) as fetch:
            espn.fetch_league_history("UCL", "uefa.champions", this_year, final=False)
            espn.fetch_league_history("UCL", "uefa.champions", this_year, final=False)
        self.assertEqual(fetch.call_count, 2)  # an in-progress season gains results weekly

    def test_an_unreachable_feed_costs_an_empty_frame_not_the_run(self):
        with mock.patch.object(espn, "get_json", return_value=None):
            df = espn.fetch_league_history("UCL", "uefa.champions", 2025, final=True)
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), espn.SOCCER_COLUMNS)


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
