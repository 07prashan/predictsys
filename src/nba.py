"""NBA basketball: Elo team ratings and a scoring-rate model, for the games ESPN lists as
upcoming - **preseason included**, which is what the site has to show in October while the
regular season is still weeks away.

Ratings come from the last few seasons of ESPN's per-team schedules. Each game appears in
both teams' schedules, so games are de-duplicated by ESPN id. Elo is the classic
margin-of-victory update (a blowout moves the ratings more than a one-point escape); the
scoring model reads each team's recency-weighted points scored / allowed and turns that into
an expected score, so the page can show a likely line and a total.

Basketball has no draws, so prob_draw is always 0 and predicted_outcome is 'H' or 'A'.

Usage (a quick look at this week's games):
    python src/nba.py
"""

import datetime as dt
from collections import defaultdict, deque
from math import erf, sqrt

import numpy as np
import pandas as pd

import espn

INITIAL_RATING = 1500.0
K = 20.0               # base Elo step
HOME_ADVANTAGE = 60.0  # Elo points - about a 58.5% home win rate between even teams
FORM_GAMES = 5

# The market's own view of scoring: NBA teams average ~114 points, and the home side scores
# a couple more than the visitor on the same night (the venue, not the teams, is the edge).
LEAGUE_AVG_POINTS = 114.0
HOME_COURT_POINTS = 3.0
# One-sigma spread of a game's combined score, for pricing an over/under around the total.
TOTAL_STD = 18.0
POINTS_HALF_LIFE_DAYS = 180.0  # scoring rates move slowly; this leans on recent form without forgetting a season
HISTORY_SEASONS = 3
ACTIVE_WITHIN_DAYS = 400  # teams not seen this recently don't belong on a ratings list


def season_year(day: dt.date) -> int:
    """The year an NBA season ENDS in - ESPN encodes 2026-27 as season 2027. Oct-Dec is the
    start of a season, Jan-Jul its back half."""
    return day.year + 1 if day.month >= 8 else day.year


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _finished_rows(records) -> list:
    """Finished games from a list of parsed ESPN records, in the history frame's shape."""
    rows = []
    for r in records:
        if r.get("state") != "post" or r.get("home_points") is None or r.get("away_points") is None:
            continue
        rows.append(
            {
                "date": pd.Timestamp(r["kickoff_utc"]).tz_convert(None).normalize(),
                "external_id": r["external_id"],
                "home": r["home"],
                "away": r["away"],
                "home_points": int(r["home_points"]),
                "away_points": int(r["away_points"]),
            }
        )
    return rows


def load_history(today: dt.date) -> pd.DataFrame:
    """Finished games over the last few seasons, from every team's own schedule."""
    current = season_year(today)
    seasons = list(range(current - (HISTORY_SEASONS - 1), current + 1))
    rows = []
    for season in seasons:
        final = season < current  # a completed season never changes, so it's cached on disk
        for team_id in espn.nba_team_ids():
            games = espn.nba_team_games(team_id, season, final=final)
            if games:
                rows += _finished_rows(games)
    if not rows:
        return pd.DataFrame(columns=["date", "external_id", "home", "away", "home_points", "away_points", "winner"])
    df = pd.DataFrame(rows).drop_duplicates("external_id").sort_values("date", kind="stable").reset_index(drop=True)
    df["winner"] = np.where(df["home_points"] > df["away_points"], "H", "A")
    print(f"  NBA: {len(df)} finished games rated across {len(seasons)} season(s), {df['home'].nunique()} teams")
    return df


class Ratings:
    """Elo for every team, keyed by the team's display name."""

    def __init__(self):
        self.overall = defaultdict(lambda: INITIAL_RATING)
        self.played = defaultdict(int)
        self.form = defaultdict(lambda: deque(maxlen=FORM_GAMES))
        self.last_played = {}

    def known(self, team: str) -> bool:
        return team in self.played

    def rating(self, team: str) -> float:
        return self.overall[team]

    def update(self, home: str, away: str, home_points: int, away_points: int, when) -> None:
        elo_home, elo_away = self.overall[home], self.overall[away]
        expected = 1.0 / (1.0 + 10 ** (-((elo_home + HOME_ADVANTAGE) - elo_away) / 400.0))
        actual = 1.0 if home_points > away_points else 0.0
        # FiveThirtyEight's margin-of-victory multiplier: blowouts move ratings more, and a
        # heavy favourite winning narrowly counts for little.
        margin = abs(home_points - away_points)
        mov = ((margin + 3.0) ** 0.8) / (7.5 + 0.006 * ((elo_home + HOME_ADVANTAGE) - elo_away))
        move = K * mov * (actual - expected)
        self.overall[home] = elo_home + move
        self.overall[away] = elo_away - move
        self.form[home].append("W" if actual == 1.0 else "L")
        self.form[away].append("L" if actual == 1.0 else "W")
        self.last_played[home] = self.last_played[away] = when


def rate_matches(games: pd.DataFrame) -> Ratings:
    ratings = Ratings()
    for g in games.itertuples(index=False):
        ratings.update(g.home, g.away, g.home_points, g.away_points, g.date)
        ratings.played[g.home] += 1
        ratings.played[g.away] += 1
    return ratings


def win_probability(rating_home: float, rating_away: float) -> float:
    """Home win probability from the two ratings, home advantage already included."""
    return 1.0 / (1.0 + 10 ** (-((rating_home + HOME_ADVANTAGE) - rating_away) / 400.0))


def scoring_model(games: pd.DataFrame, reference: pd.Timestamp) -> tuple:
    """(offence, defence, league_average) in points, each a recency-weighted mean so a
    team's current season matters more than one two seasons back."""
    off_sum, off_w = defaultdict(float), defaultdict(float)
    def_sum, def_w = defaultdict(float), defaultdict(float)
    total_sum = total_w = 0.0
    for g in games.itertuples(index=False):
        w = 0.5 ** (max((reference - g.date).days, 0) / POINTS_HALF_LIFE_DAYS)
        off_sum[g.home] += w * g.home_points
        off_w[g.home] += w
        def_sum[g.home] += w * g.away_points
        def_w[g.home] += w
        off_sum[g.away] += w * g.away_points
        off_w[g.away] += w
        def_sum[g.away] += w * g.home_points
        def_w[g.away] += w
        total_sum += w * (g.home_points + g.away_points)
        total_w += w
    offence = {t: off_sum[t] / off_w[t] for t in off_w}
    defence = {t: def_sum[t] / def_w[t] for t in def_w}
    league_avg = (total_sum / total_w / 2.0) if total_w else LEAGUE_AVG_POINTS
    return offence, defence, league_avg


def predict_fixtures(fixtures: pd.DataFrame, ratings: Ratings, model: tuple) -> pd.DataFrame:
    offence, defence, league_avg = model
    rows = []
    for fx in fixtures.itertuples(index=False):
        home, away = fx.home, fx.away
        rh, ra = ratings.rating(home), ratings.rating(away)
        p_home = win_probability(rh, ra)

        # expected score: each side's scoring rate against the opponent's allowance rate,
        # normalised by the league average, then split by the venue's edge
        exp_home = (offence.get(home, league_avg) * defence.get(away, league_avg) / league_avg) + HOME_COURT_POINTS / 2
        exp_away = (offence.get(away, league_avg) * defence.get(home, league_avg) / league_avg) - HOME_COURT_POINTS / 2
        total = exp_home + exp_away
        line = round(total * 2) / 2
        over = 1.0 - _norm_cdf((line - total) / TOTAL_STD)

        moneyline = ("Moneyline", "Home Win" if p_home >= 0.5 else "Away Win", max(p_home, 1 - p_home))
        totals = ("Total", f"Over {line:g}" if over >= 0.5 else f"Under {line:g}", max(over, 1 - over))
        market, label, prob = max((moneyline, totals), key=lambda pick: pick[2])

        rows.append(
            {
                "sport": "basketball",
                "league": "NBA",
                "competition": fx.competition,
                "round": "",
                "external_id": fx.external_id,
                "kickoff_utc": fx.kickoff_utc,
                "match_date": pd.Timestamp(fx.kickoff_utc).strftime("%Y-%m-%d"),
                "home_team": home,
                "away_team": away,
                "home_logo": fx.home_logo,
                "away_logo": fx.away_logo,
                "prob_home": p_home,
                "prob_draw": 0.0,
                "prob_away": 1 - p_home,
                "predicted_outcome": "H" if p_home >= 0.5 else "A",
                "best_pick_market": market,
                "best_pick_label": label,
                "best_pick_prob": prob,
                "home_form": "".join(ratings.form[home]),
                "away_form": "".join(ratings.form[away]),
                "home_rating": round(rh),
                "away_rating": round(ra),
                "correct_score_home": int(round(exp_home)),
                "correct_score_away": int(round(exp_away)),
                "expected_goals_home": round(exp_home, 1),
                "expected_goals_away": round(exp_away, 1),
                "total_line": line,
                "over_prob": over,
                "under_prob": 1 - over,
                "predicted_total": round(total, 1),
                "predicted_spread": round(exp_home - exp_away, 1),
            }
        )
    return pd.DataFrame(rows)


def build(today: dt.date = None, horizon_days: int = 14) -> dict:
    """Everything the NBA side contributes to one prediction run, same shape as
    tennis.build(): predictions, results (finished games, for settling), matches (history
    for the head-to-head / profile pages) and ratings."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    now = pd.Timestamp.now(tz="UTC")
    out = {"predictions": [], "results": [], "matches": [], "ratings": []}

    history = load_history(today)
    if len(history):
        ratings = rate_matches(history)
        model = scoring_model(history, now.tz_convert(None).normalize())
    else:
        ratings, model = Ratings(), ({}, {}, LEAGUE_AVG_POINTS)

    # two days back so a game that finishes overnight can still settle a stored prediction
    scoreboard = espn.fetch_nba(today - dt.timedelta(days=2), today + dt.timedelta(days=horizon_days + 1))
    if len(scoreboard):
        upcoming = scoreboard[
            (scoreboard["state"] == "pre")
            & (scoreboard["kickoff_utc"] >= now)
            & (scoreboard["kickoff_utc"] < now + pd.Timedelta(days=horizon_days))
        ]
        if len(upcoming):
            out["predictions"].append(predict_fixtures(upcoming, ratings, model))
        done = scoreboard[(scoreboard["state"] == "post")].dropna(subset=["home_points", "away_points"])
        if len(done):
            out["results"].append(
                pd.DataFrame(
                    {
                        "external_id": done["external_id"].to_numpy(),
                        "outcome": np.where(done["home_points"] > done["away_points"], "H", "A"),
                        "home_score": done["home_points"].astype(int).to_numpy(),
                        "away_score": done["away_points"].astype(int).to_numpy(),
                    }
                )
            )

    last = ratings.last_played
    cutoff = (history["date"].max() - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)) if len(history) else now.tz_convert(None)
    out["ratings"].append(
        pd.DataFrame(
            [
                {"league": "NBA", "team": team, "elo_rating": ratings.overall[team]}
                for team in ratings.played
                if last.get(team, cutoff) >= cutoff
            ]
        )
    )
    if len(history):
        shown = history[history["date"] >= pd.Timestamp(today) - pd.DateOffset(years=4)]
        out["matches"].append(
            pd.DataFrame(
                {
                    "league": "NBA",
                    "date": shown["date"].dt.strftime("%Y-%m-%d").to_numpy(),
                    "home_team": shown["home"].to_numpy(),
                    "away_team": shown["away"].to_numpy(),
                    "home_goals": shown["home_points"].to_numpy(),
                    "away_goals": shown["away_points"].to_numpy(),
                    "result": shown["winner"].to_numpy(),
                }
            )
        )

    def concat(key, empty):
        return pd.concat(out[key], ignore_index=True) if out[key] else empty

    return {
        "predictions": concat("predictions", pd.DataFrame()),
        "results": concat("results", pd.DataFrame(columns=["external_id", "outcome", "home_score", "away_score"])),
        "matches": concat("matches", pd.DataFrame(columns=["league", "date", "home_team", "away_team", "home_goals", "away_goals", "result"])),
        "ratings": concat("ratings", pd.DataFrame(columns=["league", "team", "elo_rating"])),
    }


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    result = build()
    preds = result["predictions"]
    if len(preds):
        print(
            preds[["kickoff_utc", "competition", "home_team", "away_team", "prob_home", "predicted_outcome",
                   "correct_score_home", "correct_score_away", "best_pick_label"]].to_string(index=False)
        )
    else:
        print("No upcoming NBA games found.")
