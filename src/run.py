"""Fetch results for all covered European leagues, engineer features, and
walk-forward backtest naive vs Elo vs gradient boosting vs the market.

Usage:
    python src/run.py
"""

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from xgboost import XGBClassifier

from backtest import OUTCOME_CODES, OUTCOMES, decay_weights, walk_forward_backtest
from elo import EloConfig, run_ratings
from features import run_features
from fetch_data import LEAGUES, fetch_seasons
from poisson_model import poisson_walk_forward
from xg import fetch_xg

SEASON_START_YEARS = range(2017, 2026)  # 2017/18 through 2025/26

# tune.py's search results - shared here so predict.py fits the exact same models
# it was validated with, instead of a second copy of these numbers drifting out of sync.
# Re-tuned on all 11 leagues (originally just the top 5) - the Elo config and Poisson
# half-life came back identical, but gbm's search picked a 4-year recency half-life
# this time, where the 5-league search found no decay helped at all.
TUNED_ELO_CONFIG = EloConfig(k_factor=30, home_advantage=50, promoted_penalty=100)
POISSON_HALF_LIFE_DAYS = 730
GBM_DECAY_HALF_LIFE_DAYS = 1460

ELO_FEATURES = ["elo_diff", "abs_elo_diff"]
GBM_FEATURES = ELO_FEATURES + [
    # recent form, any venue - "how hot is this team right now"
    "home_form_ppg", "away_form_ppg",
    "home_form_gd", "away_form_gd",
    # attack/defense strength, venue-specific - "how good is this team AT HOME / AWAY specifically"
    "home_attack_home", "home_defense_home",
    "away_attack_away", "away_defense_away",
    # fixture congestion
    "home_rest_days", "away_rest_days",
    # history between these two specific teams
    "h2h_home_strength", "h2h_goal_diff_avg", "h2h_matches_count",
    # trending up or down, beyond the current Elo level itself
    "home_elo_momentum", "away_elo_momentum",
    # this season's table position so far
    "home_season_ppg", "away_season_ppg",
    "home_season_played", "away_season_played",
    # chance quality (xG), venue-specific - smoother than actual goals, which are noisy in small samples
    "home_xg_attack_home", "home_xg_defense_home",
    "away_xg_attack_away", "away_xg_defense_away",
    # which league, so the model can share signal across all five while still specializing
    "league",
]

# NOT included: player availability (injuries/suspensions) and individual player form.
# football-data.co.uk is match-level only. Getting this would need a lineup/injury data
# source (e.g. a paid api-football.com plan) with reliable bulk history across five
# leagues and nine seasons - worth doing deliberately, not bolted on with fragile scraping.


def load_league(league: str, season_start_years=None, force_years: set = None) -> pd.DataFrame:
    season_start_years = season_start_years if season_start_years is not None else SEASON_START_YEARS
    paths = fetch_seasons(season_start_years, league=league, force_years=force_years)
    frames = []
    for path, start_year in zip(paths, season_start_years):
        if path is None:  # this league's season hasn't started yet - nothing to load
            continue
        try:
            df = pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            # a handful of these files have a stray non-UTF-8 byte (seen: a Windows-1252
            # non-breaking space inside an odds column we don't even use) - cp1252 can
            # decode any byte sequence, so it never throws, at the cost of possibly
            # mis-rendering a genuine non-ASCII character in some unused column
            df = pd.read_csv(path, encoding="cp1252")
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTR"]).copy()
        # format="mixed": older seasons use 2-digit years, newer ones use 4-digit years
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
        df["season"] = start_year
        frames.append(df)
    matches = pd.concat(frames, ignore_index=True)
    return matches.sort_values("Date").reset_index(drop=True)


def summarize(results: pd.DataFrame, models: list) -> dict:
    """Weighted (by matches played) accuracy/log-loss per model, across all test seasons."""
    summary = {}
    for model in models:
        acc_col, ll_col = f"{model}_accuracy", f"{model}_logloss"
        valid = results.dropna(subset=[acc_col, ll_col])
        if valid.empty:
            summary[model] = (None, None)
            continue
        weights = valid["n_matches"]
        summary[model] = (
            (valid[acc_col] * weights).sum() / weights.sum(),
            (valid[ll_col] * weights).sum() / weights.sum(),
        )
    return summary


def load_raw_matches(season_start_years=None, force_years: set = None) -> dict:
    """Per league: match results with xG merged in, before Elo or feature engineering.
    Fetches/caches on first call; cheap to call repeatedly after that - this is what
    lets tune.py try many different Elo configs without re-fetching anything.

    force_years re-fetches those specific seasons even if cached - predict.py uses this
    for the in-progress season, which gains new results every week."""
    season_start_years = season_start_years if season_start_years is not None else SEASON_START_YEARS
    raw = {}
    for code in LEAGUES:
        matches = load_league(code, season_start_years, force_years)
        xg = fetch_xg(code, season_start_years, force_years)
        raw[code] = matches.merge(xg, on=["season", "HomeTeam", "AwayTeam"], how="left")
    return raw


def build_dataset(elo_config: EloConfig = None, raw: dict = None, verbose: bool = True, return_state: bool = False):
    """Per league: rate teams with Elo and build form/rest/h2h features, all kept
    scoped to that league's own matches (these leagues never play each other, so
    a shared rating or form pool across them would mix unrelated competitions).
    The results are then pooled into one table so the gradient-boosted model can
    share statistical strength across leagues via a 'league' feature.

    With return_state=True, also returns {league_code: (EloRatings, FeatureTracker)} -
    each holding its FINAL state after all of `raw`, ready to answer live queries for
    fixtures beyond it. That's what predict.py uses."""
    raw = raw if raw is not None else load_raw_matches()
    per_league = []
    state = {}
    for code, name in LEAGUES.items():
        matches = raw[code]
        rated, engine = run_ratings(matches, elo_config)
        featured, tracker = run_features(rated)
        featured["league"] = code
        per_league.append(featured)
        state[code] = (engine, tracker)
        if verbose:
            matched = matches["home_xg"].notna().mean()
            top3 = sorted(engine.ratings.items(), key=lambda kv: kv[1], reverse=True)[:3]
            print(
                f"{name:<28} {len(matches):>5} matches   xG matched: {matched:>5.1%}   top 3: "
                + ", ".join(f"{t} ({r:.0f})" for t, r in top3)
            )
    dataset = pd.concat(per_league, ignore_index=True)
    return (dataset, state) if return_state else dataset


def make_gbm() -> XGBClassifier:
    # hyperparameters from tune.py's random search (re-run on all 11 leagues), chosen
    # using only seasons before 2022 and confirmed to actually help on the untouched
    # 2022+ seasons. This search picked GBM_DECAY_HALF_LIFE_DAYS as helpful - unlike the
    # original 5-league search, which found no recency decay helped at all - so it must
    # actually be applied via sample_weight wherever this model is fit (see predict.py
    # and backtest.decay_weights()), not just declared here and forgotten.
    return XGBClassifier(
        n_estimators=500,
        max_depth=2,
        learning_rate=0.02,
        min_child_weight=10,
        subsample=0.7,
        colsample_bytree=0.6,
        objective="multi:softprob",
        eval_metric="mlogloss",
        tree_method="hist",
        enable_categorical=True,
    )


def ensemble_results(all_matches: pd.DataFrame, proba: dict, members: list) -> pd.DataFrame:
    """Averages several models' predicted probabilities match-by-match, then
    scores that average per season - a plain average has no fitted parameters
    of its own, so there's nothing here that could leak the future."""
    index = proba[members[0]].index
    avg = sum(proba[m].reindex(index) for m in members) / len(members)
    # each member's own predict_proba can be off by a floating-point hair (e.g. XGBoost's
    # float32 softmax lands at 0.9999999, not exactly 1.0); re-normalize the average so
    # log_loss always sees rows that sum to exactly 1, regardless of that upstream noise
    avg = avg.div(avg.sum(axis=1), axis=0)

    rows = []
    seasons = all_matches.loc[index, "season"]
    for season in sorted(seasons.unique()):
        idx = index[seasons == season]
        y_true, p = all_matches.loc[idx, "FTR"], avg.loc[idx]
        rows.append(
            {
                "season": season,
                "n_matches": len(idx),
                "ensemble_accuracy": accuracy_score(y_true, p.idxmax(axis=1)),
                "ensemble_logloss": log_loss(y_true, p[OUTCOMES], labels=OUTCOMES),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")

    print("=== Loading and rating all leagues ===\n")
    all_matches = build_dataset(elo_config=TUNED_ELO_CONFIG)
    all_matches["league"] = all_matches["league"].astype("category")
    print(f"\nPooled dataset: {len(all_matches)} matches.\n")

    # elo and gbm are scored in SEPARATE calls, not one shared one, because
    # decay_half_life_days applies to every model passed to a single call - gbm's
    # tuned decay must never leak onto elo, which was never validated with any decay.
    print("=== Walk-forward backtest: Elo and gradient boosting ===\n")
    elo_results, elo_proba = walk_forward_backtest(
        all_matches, {"elo": (LogisticRegression(max_iter=1000), ELO_FEATURES)}, return_proba=True
    )
    gbm_results, gbm_proba = walk_forward_backtest(
        all_matches,
        {"gbm": (make_gbm(), GBM_FEATURES)},
        return_proba=True,
        decay_half_life_days=GBM_DECAY_HALF_LIFE_DAYS,
    )
    results = elo_results.merge(
        gbm_results[["season", "n_matches", "gbm_accuracy", "gbm_logloss"]], on=["season", "n_matches"]
    )
    proba = {**elo_proba, "gbm": gbm_proba["gbm"]}
    print(results.to_string(index=False))

    print("\n=== Walk-forward backtest: Poisson goal-scoring model ===\n")
    # POISSON_HALF_LIFE_DAYS from tune.py's search - recency weighting genuinely helped here
    # (unlike gbm). Dixon-Coles's low-score correlation term was tested via an ablation and
    # made log-loss slightly worse, not better, so it's left off (use_dixon_coles=False).
    poisson_results, poisson_proba = poisson_walk_forward(
        all_matches, half_life_days=POISSON_HALF_LIFE_DAYS, use_dixon_coles=False
    )
    print(poisson_results.to_string(index=False))
    proba["poisson"] = poisson_proba

    print("\n=== Ensemble: plain average of Elo + gbm + Poisson probabilities ===\n")
    ens_results = ensemble_results(all_matches, proba, ["elo", "gbm", "poisson"])
    print(ens_results.to_string(index=False))

    combined = results.merge(poisson_results, on=["season", "n_matches"]).merge(
        ens_results, on=["season", "n_matches"]
    )

    print("\n=== Aggregate across all test seasons (weighted by matches played) ===")
    summary = summarize(combined, ["elo", "gbm", "poisson", "ensemble", "naive", "market"])
    for model, (acc, ll) in summary.items():
        if acc is not None:
            print(f"  {model:>9}: accuracy={acc:.3f}  log_loss={ll:.3f}")

    print("\n=== What the gradient-boosted model actually leans on ===")
    final_clf = make_gbm()
    weights = decay_weights(all_matches["Date"], all_matches["Date"].max(), GBM_DECAY_HALF_LIFE_DAYS)
    final_clf.fit(all_matches[GBM_FEATURES], all_matches["FTR"].map(OUTCOME_CODES), sample_weight=weights)
    importances = sorted(zip(GBM_FEATURES, final_clf.feature_importances_), key=lambda kv: -kv[1])
    for feature, importance in importances:
        print(f"  {feature:<18} {importance:.3f}")


if __name__ == "__main__":
    main()
