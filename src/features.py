"""Pre-match features built entirely from match history: form, head-to-head,
home/away splits, attack/defense strength, table position, and Elo momentum.

Every value here is computed strictly from matches that happened before the
match in question - state for a team or pairing is only updated AFTER that
match's row has read it. Where a team's history is too short to compute a
feature (start of the dataset, first-ever meeting between two teams, start
of a season), the feature is left as NaN rather than backfilled with a
made-up placeholder; the gradient-boosted model handles missing values
natively rather than being fed a fabricated number.

FeatureTracker splits the old single-pass function into read()/update() so
the same state machine serves two jobs: replaying history to build training
features (run_features, used by backtesting), and answering "what would this
feature be for a fixture that hasn't happened yet" for live prediction -
read() never mutates state, so it's safe to call for a fixture and then never
follow up with update() until that fixture actually has a result.

Player-level signals (who's injured or suspended, individual player form)
are deliberately NOT included here - see the note in run.py. They need a
lineup/injury data source we haven't wired up, and none of the free options
give reliable bulk history across all our leagues and nine-plus seasons.
"""

from collections import defaultdict, deque

import pandas as pd

POINTS = {"H": (3, 0), "D": (1, 1), "A": (0, 3)}  # (home_points, away_points)
RESULT_SCORE = {"H": 1.0, "D": 0.5, "A": 0.0}  # score for the home side of that historical meeting

FEATURE_COLUMNS = (
    "home_form_ppg", "away_form_ppg",
    "home_form_gd", "away_form_gd",
    "home_rest_days", "away_rest_days",
    "h2h_home_strength", "h2h_matches_count", "h2h_goal_diff_avg",
    "home_attack_home", "home_defense_home",
    "away_attack_away", "away_defense_away",
    "home_elo_momentum", "away_elo_momentum",
    "home_season_ppg", "away_season_ppg",
    "home_season_played", "away_season_played",
    "home_xg_attack_home", "home_xg_defense_home",
    "away_xg_attack_away", "away_xg_defense_away",
)


def _avg(values) -> float:
    return sum(values) / len(values) if values else float("nan")


class FeatureTracker:
    def __init__(self, form_window: int = 5, h2h_window: int = 5, momentum_window: int = 5):
        self.momentum_window = momentum_window

        self.recent = defaultdict(lambda: deque(maxlen=form_window))  # team -> deque[(points, goal_diff)]
        self.recent_home_venue = defaultdict(lambda: deque(maxlen=form_window))  # team's last N AS HOME
        self.recent_away_venue = defaultdict(lambda: deque(maxlen=form_window))  # team's last N AS AWAY
        self.xg_home_venue = defaultdict(lambda: deque(maxlen=form_window))  # team -> deque[(xg_for, xg_against)]
        self.xg_away_venue = defaultdict(lambda: deque(maxlen=form_window))
        self.last_played = {}  # team -> date of that team's last match
        self.h2h_score = defaultdict(lambda: deque(maxlen=h2h_window))  # (team_a, team_b) sorted -> deque[score_for_a]
        self.h2h_gd = defaultdict(lambda: deque(maxlen=h2h_window))
        self.elo_history = defaultdict(lambda: deque(maxlen=momentum_window))  # team -> deque of past pre-match ratings
        self.season_table = {}  # team -> {"points": int, "played": int}
        self.current_season = None

    def read(self, home: str, away: str, date, season, elo_home: float, elo_away: float) -> dict:
        """Pre-match features for `home` vs `away` on `date`. Pure read - never mutates
        state, so it's safe to call for a fixture that hasn't been played yet."""
        feats = {}

        h_hist, a_hist = self.recent[home], self.recent[away]
        feats["home_form_ppg"] = _avg([p for p, _ in h_hist])
        feats["away_form_ppg"] = _avg([p for p, _ in a_hist])
        feats["home_form_gd"] = _avg([g for _, g in h_hist])
        feats["away_form_gd"] = _avg([g for _, g in a_hist])

        feats["home_rest_days"] = (date - self.last_played[home]).days if home in self.last_played else float("nan")
        feats["away_rest_days"] = (date - self.last_played[away]).days if away in self.last_played else float("nan")

        team_a, team_b = sorted((home, away))
        pair_scores, pair_gds = self.h2h_score[(team_a, team_b)], self.h2h_gd[(team_a, team_b)]
        feats["h2h_matches_count"] = len(pair_scores)
        if pair_scores:
            avg_score_a, avg_gd_a = _avg(pair_scores), _avg(pair_gds)
            feats["h2h_home_strength"] = avg_score_a if home == team_a else 1 - avg_score_a
            feats["h2h_goal_diff_avg"] = avg_gd_a if home == team_a else -avg_gd_a
        else:
            feats["h2h_home_strength"] = float("nan")
            feats["h2h_goal_diff_avg"] = float("nan")

        home_venue_hist, away_venue_hist = self.recent_home_venue[home], self.recent_away_venue[away]
        feats["home_attack_home"] = _avg([gf for _, gf, _ga in home_venue_hist])
        feats["home_defense_home"] = _avg([ga for _, _gf, ga in home_venue_hist])
        feats["away_attack_away"] = _avg([gf for _, gf, _ga in away_venue_hist])
        feats["away_defense_away"] = _avg([ga for _, _gf, ga in away_venue_hist])

        h_xg_hist, a_xg_hist = self.xg_home_venue[home], self.xg_away_venue[away]
        feats["home_xg_attack_home"] = _avg([xf for xf, _xa in h_xg_hist])
        feats["home_xg_defense_home"] = _avg([xa for _xf, xa in h_xg_hist])
        feats["away_xg_attack_away"] = _avg([xf for xf, _xa in a_xg_hist])
        feats["away_xg_defense_away"] = _avg([xa for _xf, xa in a_xg_hist])

        home_elo_hist, away_elo_hist = self.elo_history[home], self.elo_history[away]
        feats["home_elo_momentum"] = (
            elo_home - home_elo_hist[0] if len(home_elo_hist) == self.momentum_window else float("nan")
        )
        feats["away_elo_momentum"] = (
            elo_away - away_elo_hist[0] if len(away_elo_hist) == self.momentum_window else float("nan")
        )

        # a query for a season this tracker hasn't reached yet (or has already moved past)
        # sees an empty table - the season is only ever partially known "as of" its own matches
        table = self.season_table if season == self.current_season else {}
        home_table = table.get(home, {"points": 0, "played": 0})
        away_table = table.get(away, {"points": 0, "played": 0})
        feats["home_season_ppg"] = home_table["points"] / home_table["played"] if home_table["played"] else float("nan")
        feats["away_season_ppg"] = away_table["points"] / away_table["played"] if away_table["played"] else float("nan")
        feats["home_season_played"] = home_table["played"]
        feats["away_season_played"] = away_table["played"]

        return feats

    def update(self, row) -> None:
        """Advances every tracker using a match that has actually happened. row needs
        HomeTeam, AwayTeam, Date, season, FTR, FTHG, FTAG, elo_home, elo_away, and
        optionally home_xg/away_xg."""
        home, away, date = row.HomeTeam, row.AwayTeam, row.Date

        if row.season != self.current_season:
            self.season_table = {}
            self.current_season = row.season

        home_pts, away_pts = POINTS[row.FTR]
        goal_diff = row.FTHG - row.FTAG

        self.recent[home].append((home_pts, goal_diff))
        self.recent[away].append((away_pts, -goal_diff))
        self.recent_home_venue[home].append((home_pts, row.FTHG, row.FTAG))
        self.recent_away_venue[away].append((away_pts, row.FTAG, row.FTHG))
        self.last_played[home] = date
        self.last_played[away] = date

        # only record xG when this specific match actually has it - never fabricate a value,
        # and never let a missing entry silently poison the rolling average with a false zero
        home_xg, away_xg = getattr(row, "home_xg", None), getattr(row, "away_xg", None)
        if pd.notna(home_xg) and pd.notna(away_xg):
            self.xg_home_venue[home].append((home_xg, away_xg))
            self.xg_away_venue[away].append((away_xg, home_xg))

        team_a, team_b = sorted((home, away))
        score_a = RESULT_SCORE[row.FTR] if home == team_a else 1 - RESULT_SCORE[row.FTR]
        gd_a = goal_diff if home == team_a else -goal_diff
        self.h2h_score[(team_a, team_b)].append(score_a)
        self.h2h_gd[(team_a, team_b)].append(gd_a)

        self.elo_history[home].append(row.elo_home)
        self.elo_history[away].append(row.elo_away)

        home_table = self.season_table.get(home, {"points": 0, "played": 0})
        away_table = self.season_table.get(away, {"points": 0, "played": 0})
        self.season_table[home] = {"points": home_table["points"] + home_pts, "played": home_table["played"] + 1}
        self.season_table[away] = {"points": away_table["points"] + away_pts, "played": away_table["played"] + 1}


def run_features(matches: pd.DataFrame, form_window: int = 5, h2h_window: int = 5, momentum_window: int = 5):
    """matches must be one league's rows, sorted chronologically, with elo_home/elo_away
    already attached. Returns (matches_with_features, tracker) - the tracker holds every
    team's final state, ready to answer FeatureTracker.read() for fixtures beyond `matches`."""
    tracker = FeatureTracker(form_window, h2h_window, momentum_window)
    cols = {k: [] for k in FEATURE_COLUMNS}

    for row in matches.itertuples():
        feats = tracker.read(row.HomeTeam, row.AwayTeam, row.Date, row.season, row.elo_home, row.elo_away)
        for k, v in feats.items():
            cols[k].append(v)
        tracker.update(row)

    result = matches.copy()
    for col, values in cols.items():
        result[col] = values
    return result, tracker
