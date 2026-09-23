"""A Dixon-Coles-style team-strength Poisson model, with recency-weighted fitting.

Each team gets an attack rating and a defense rating (fit jointly via a
weighted Poisson GLM: goals ~ team + opponent + is_home), plus one home-
advantage term - fit separately per league, per walk-forward fold, using
only that fold's training matches. This models the goal-scoring PROCESS
directly, which is a genuinely different approach from Elo or the gradient-
boosted model: both of those predict the discrete H/D/A outcome straight
from features, while this predicts each side's expected goals and derives
match outcome probabilities by summing over the resulting scoreline grid.

Two refinements on top of a plain independent-Poisson model:

1. Dixon-Coles low-score correlation (rho). Two independent Poisson draws
   slightly overstate 1-0/0-1 and understate 0-0/1-1 relative to what
   actually happens - real matches have a small negative correlation
   between the two side's scores at low scorelines (cautious/negative
   matches tend to stay low on both sides together). rho is fit per
   league/fold by maximizing the likelihood of the four affected scorelines
   with lambda/mu held fixed from the GLM fit - a fast profile-likelihood
   approximation of the full joint Dixon-Coles MLE, not the textbook
   version, but close enough to be worth the ~15 lines of code it costs.

2. Recency weighting. A match from four seasons ago probably says less
   about a team's CURRENT strength than one from last month, but a plain
   GLM fit weighs every training match identically. Matches are down-
   weighted by an exponential half-life relative to the test period, via
   statsmodels' var_weights (verified against an unweighted fit - weight=1
   everywhere reproduces the unweighted result exactly).
"""

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy.optimize import minimize_scalar
from scipy.stats import poisson
from sklearn.metrics import accuracy_score, log_loss

OUTCOMES = ["A", "D", "H"]  # same convention as backtest.py


def _recency_weights(dates: pd.Series, as_of, half_life_days: float) -> np.ndarray:
    days_before = (as_of - dates).dt.days.clip(lower=0)
    return 0.5 ** (days_before / half_life_days)


def _fit_one_league(train: pd.DataFrame, as_of, half_life_days: float = None):
    n = len(train)
    home_rows = pd.DataFrame(
        {"team": train["HomeTeam"], "opponent": train["AwayTeam"], "goals": train["FTHG"], "is_home": 1}
    )
    away_rows = pd.DataFrame(
        {"team": train["AwayTeam"], "opponent": train["HomeTeam"], "goals": train["FTAG"], "is_home": 0}
    )
    long = pd.concat([home_rows, away_rows], ignore_index=True)

    fit_kwargs = {}
    if half_life_days is not None:
        w = _recency_weights(train["Date"], as_of, half_life_days).to_numpy()
        fit_kwargs["var_weights"] = np.concatenate([w, w])  # home_rows and away_rows share the same match weight

    model = smf.glm("goals ~ team + opponent + is_home", data=long, family=sm.families.Poisson(), **fit_kwargs).fit()
    lambda_home = model.fittedvalues.iloc[:n].to_numpy()
    mu_away = model.fittedvalues.iloc[n:].to_numpy()
    return model, lambda_home, mu_away


def _dc_tau(x: np.ndarray, y: np.ndarray, lam: np.ndarray, mu: np.ndarray, rho: float) -> np.ndarray:
    tau = np.ones_like(lam, dtype=float)
    tau = np.where((x == 0) & (y == 0), 1 - lam * mu * rho, tau)
    tau = np.where((x == 0) & (y == 1), 1 + lam * rho, tau)
    tau = np.where((x == 1) & (y == 0), 1 + mu * rho, tau)
    tau = np.where((x == 1) & (y == 1), 1 - rho, tau)
    return tau


def _fit_rho(train: pd.DataFrame, lambda_home: np.ndarray, mu_away: np.ndarray) -> float:
    xs, ys = train["FTHG"].to_numpy(), train["FTAG"].to_numpy()

    def neg_log_lik(rho):
        tau = _dc_tau(xs, ys, lambda_home, mu_away, rho)
        return -np.sum(np.log(np.clip(tau, 1e-10, None)))

    return minimize_scalar(neg_log_lik, bounds=(-0.5, 0.5), method="bounded").x


def _lambda(model, team: str, opponent: str, is_home: int, known_teams: set, league_avg: float) -> float:
    """Falls back to the league's average goals-per-team-per-match if either side
    never appeared in training (e.g. a newly promoted team) - the fitted model has
    no coefficient for a team it has never seen."""
    if team not in known_teams or opponent not in known_teams:
        return league_avg
    row = pd.DataFrame({"team": [team], "opponent": [opponent], "is_home": [is_home]})
    return float(model.predict(row).iloc[0])


def _full_market_probs(lam_home: float, lam_away: float, rho: float, max_goals: int = 10) -> dict:
    """Every goal-based market comes off the same scoreline grid, computed once: match
    result, the single most likely correct score, both-teams-to-score, over/under at a
    few thresholds, double chance, and clean sheets - roughly what a real prediction
    app shows per match, all derived from one Poisson grid rather than fabricated."""
    home_pmf = poisson.pmf(np.arange(max_goals + 1), lam_home)
    away_pmf = poisson.pmf(np.arange(max_goals + 1), lam_away)
    grid = np.outer(home_pmf, away_pmf)  # grid[i, j] = P(home scores i, away scores j)

    grid[0, 0] *= 1 - lam_home * lam_away * rho
    grid[0, 1] *= 1 + lam_home * rho
    grid[1, 0] *= 1 + lam_away * rho
    grid[1, 1] *= 1 - rho
    grid = np.clip(grid, 0, None)  # guard against a pathological rho making a cell negative
    grid = grid / grid.sum()  # renormalize once, covers the sliver of mass beyond max_goals too

    p_home = np.tril(grid, -1).sum()
    p_draw = np.trace(grid)
    p_away = np.triu(grid, 1).sum()

    best_home, best_away = np.unravel_index(np.argmax(grid), grid.shape)
    total_goals = np.add.outer(np.arange(max_goals + 1), np.arange(max_goals + 1))

    return {
        "A": p_away,
        "D": p_draw,
        "H": p_home,
        "expected_goals_home": lam_home,
        "expected_goals_away": lam_away,
        "correct_score_home": int(best_home),
        "correct_score_away": int(best_away),
        "correct_score_prob": float(grid[best_home, best_away]),
        "btts_yes_prob": float(grid[1:, 1:].sum()),  # both sides score at least once
        "over_1_5_prob": float(grid[total_goals > 1.5].sum()),
        "over_2_5_prob": float(grid[total_goals > 2.5].sum()),
        "over_3_5_prob": float(grid[total_goals > 3.5].sum()),
        "double_chance_1x_prob": float(p_home + p_draw),  # home win or draw
        "double_chance_x2_prob": float(p_draw + p_away),  # draw or away win
        "double_chance_12_prob": float(p_home + p_away),  # either team wins
        "home_clean_sheet_prob": float(grid[:, 0].sum()),  # away side scores 0
        "away_clean_sheet_prob": float(grid[0, :].sum()),  # home side scores 0
    }


def _outcome_probs(lam_home: float, lam_away: float, rho: float, max_goals: int = 10) -> tuple:
    full = _full_market_probs(lam_home, lam_away, rho, max_goals)
    return full["A"], full["D"], full["H"]  # order matches OUTCOMES = [A, D, H]


def poisson_walk_forward(
    matches: pd.DataFrame,
    season_col: str = "season",
    half_life_days: float = None,
    use_dixon_coles: bool = True,
) -> tuple:
    """Same walk-forward contract as backtest.walk_forward_backtest: refit fresh
    on every season strictly before the test season, never touching the future.
    Fit PER LEAGUE (a league's `matches` here can safely span all five pooled -
    teams from different leagues never share a rating, same as Elo).

    Returns (results_df, proba_df) - proba_df holds every test-fold prediction,
    indexed like `matches`, so it can be combined with other models' predictions
    into an ensemble.
    """
    seasons = sorted(matches[season_col].unique())
    rows = []
    all_proba = []

    for i in range(1, len(seasons)):
        train = matches[matches[season_col].isin(seasons[:i])]
        test = matches[matches[season_col] == seasons[i]]
        if train.empty or test.empty:
            continue
        as_of = test["Date"].min()

        fold_proba = pd.DataFrame(index=test.index, columns=OUTCOMES, dtype=float)
        for league in test["league"].unique():
            league_train = train[train["league"] == league]
            league_test = test[test["league"] == league]
            if league_train.empty or league_test.empty:
                continue

            model, lambda_home, mu_away = _fit_one_league(league_train, as_of, half_life_days)
            rho = _fit_rho(league_train, lambda_home, mu_away) if use_dixon_coles else 0.0
            known_teams = set(league_train["HomeTeam"]) | set(league_train["AwayTeam"])
            league_avg = pd.concat([league_train["FTHG"], league_train["FTAG"]]).mean()

            for idx, row in league_test.iterrows():
                lam_h = _lambda(model, row["HomeTeam"], row["AwayTeam"], 1, known_teams, league_avg)
                lam_a = _lambda(model, row["AwayTeam"], row["HomeTeam"], 0, known_teams, league_avg)
                fold_proba.loc[idx] = _outcome_probs(lam_h, lam_a, rho)

        rows.append(
            {
                "season": seasons[i],
                "n_matches": len(test),
                "poisson_accuracy": accuracy_score(test["FTR"], fold_proba.idxmax(axis=1)),
                "poisson_logloss": log_loss(test["FTR"], fold_proba[OUTCOMES], labels=OUTCOMES),
            }
        )
        all_proba.append(fold_proba)

    return pd.DataFrame(rows), pd.concat(all_proba)


def fit_for_prediction(matches: pd.DataFrame, half_life_days: float = None, use_dixon_coles: bool = False) -> dict:
    """Fits one model per league on ALL of `matches` (no held-out split - this is for
    predicting fixtures that haven't happened yet, not for backtesting). Returns
    {league: (model, known_teams, league_avg, rho)}, consumed by predict_fixtures()."""
    as_of = matches["Date"].max()
    league_models = {}
    for league in matches["league"].unique():
        league_train = matches[matches["league"] == league]
        model, lambda_home, mu_away = _fit_one_league(league_train, as_of, half_life_days)
        rho = _fit_rho(league_train, lambda_home, mu_away) if use_dixon_coles else 0.0
        known_teams = set(league_train["HomeTeam"]) | set(league_train["AwayTeam"])
        league_avg = pd.concat([league_train["FTHG"], league_train["FTAG"]]).mean()
        league_models[league] = (model, known_teams, league_avg, rho)
    return league_models


MARKET_COLUMNS = [
    "A", "D", "H",
    "expected_goals_home", "expected_goals_away",
    "correct_score_home", "correct_score_away", "correct_score_prob",
    "btts_yes_prob", "over_1_5_prob", "over_2_5_prob", "over_3_5_prob",
    "double_chance_1x_prob", "double_chance_x2_prob", "double_chance_12_prob",
    "home_clean_sheet_prob", "away_clean_sheet_prob",
]


def predict_fixtures(fixtures: pd.DataFrame, league_models: dict) -> pd.DataFrame:
    """Every goal-based market (result, correct score, BTTS, over/under) for `fixtures`
    (needs league, HomeTeam, AwayTeam), using models already fit by fit_for_prediction()."""
    markets = pd.DataFrame(index=fixtures.index, columns=MARKET_COLUMNS, dtype=float)
    for idx, row in fixtures.iterrows():
        if row["league"] not in league_models:
            continue
        model, known_teams, league_avg, rho = league_models[row["league"]]
        lam_h = _lambda(model, row["HomeTeam"], row["AwayTeam"], 1, known_teams, league_avg)
        lam_a = _lambda(model, row["AwayTeam"], row["HomeTeam"], 0, known_teams, league_avg)
        markets.loc[idx] = _full_market_probs(lam_h, lam_a, rho)
    return markets
