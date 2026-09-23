"""A minimal Elo rating engine for football (soccer) match outcomes."""

from dataclasses import dataclass

import pandas as pd


@dataclass
class EloConfig:
    k_factor: float = 20.0
    home_advantage: float = 60.0
    initial_rating: float = 1500.0
    # A team appearing for the first time after the dataset's first season is
    # presumed promoted, and real promoted teams are usually weaker than the
    # league average - starting them at the flat initial_rating overrates them
    # until enough matches correct it. Doesn't apply to the very first season,
    # where every team is unseen and there's no information to penalize anyone with.
    promoted_penalty: float = 100.0


class EloRatings:
    def __init__(self, config: EloConfig = None):
        self.config = config or EloConfig()
        self.ratings: dict[str, float] = {}
        self.penalize_new_teams = False  # flipped on by run_ratings once the first season has passed

    def get(self, team: str) -> float:
        if team in self.ratings:
            return self.ratings[team]
        if self.penalize_new_teams:
            return self.config.initial_rating - self.config.promoted_penalty
        return self.config.initial_rating

    def expected_home_score(self, home: str, away: str) -> float:
        diff = self.get(home) + self.config.home_advantage - self.get(away)
        return 1.0 / (1.0 + 10 ** (-diff / 400.0))

    def update(self, home: str, away: str, result: str) -> None:
        """result: 'H', 'D', or 'A' - the full-time result from the home team's side."""
        actual_home = {"H": 1.0, "D": 0.5, "A": 0.0}[result]
        expected_home = self.expected_home_score(home, away)
        delta = self.config.k_factor * (actual_home - expected_home)
        self.ratings[home] = self.get(home) + delta
        self.ratings[away] = self.get(away) - delta


def run_ratings(matches: pd.DataFrame, config: EloConfig = None) -> tuple[pd.DataFrame, EloRatings]:
    """
    matches must be sorted chronologically with columns HomeTeam, AwayTeam, FTR, season.

    Ratings are read for each match BEFORE that match updates them, so the
    returned elo_home/elo_away/elo_diff columns never leak the match's own result.
    """
    config = config or EloConfig()
    engine = EloRatings(config)
    first_season = matches["season"].iloc[0] if "season" in matches.columns and len(matches) else None

    elo_home, elo_away = [], []
    for row in matches.itertuples():
        if first_season is not None and row.season != first_season:
            engine.penalize_new_teams = True
        elo_home.append(engine.get(row.HomeTeam))
        elo_away.append(engine.get(row.AwayTeam))
        engine.update(row.HomeTeam, row.AwayTeam, row.FTR)

    out = matches.copy()
    out["elo_home"] = elo_home
    out["elo_away"] = elo_away
    out["elo_diff"] = out["elo_home"] + config.home_advantage - out["elo_away"]
    # a plain elo_diff can't express that draws peak when teams are evenly
    # matched rather than scaling linearly with strength difference
    out["abs_elo_diff"] = out["elo_diff"].abs()
    return out, engine
