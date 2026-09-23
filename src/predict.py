"""Predict upcoming fixtures across all covered European leagues using every model
built so far, log those predictions, and settle any earlier predictions whose
matches have since been played.

This is the one script meant to run on a schedule - each run: refreshes the
in-progress season's results, settles anything that finished since last time,
predicts what's coming up next, and stores those predictions for grading
once they, too, are settled on a future run.

Usage:
    python src/predict.py
"""

import datetime as dt

import pandas as pd
from sklearn.linear_model import LogisticRegression

import storage
from backtest import OUTCOME_CODES, OUTCOMES, decay_weights
from elo import EloConfig
from fetch_data import LEAGUES, current_season_start_year
from fetch_fixtures import fetch_fixtures
from poisson_model import fit_for_prediction, predict_fixtures
from run import (
    ELO_FEATURES,
    GBM_DECAY_HALF_LIFE_DAYS,
    GBM_FEATURES,
    POISSON_HALF_LIFE_DAYS,
    TUNED_ELO_CONFIG,
    build_dataset,
    load_raw_matches,
    make_gbm,
)


def best_pick(row: pd.Series) -> tuple:
    """Whichever single market is most confident for this match - the result (Elo+GBM+Poisson
    ensemble), both-teams-to-score, or over/under 2.5 goals (both Poisson-only, since Elo and
    GBM predict the discrete result, not goals). Returns (market, label, probability)."""
    result = max(
        [("Result", "Home Win", row["prob_home"]), ("Result", "Draw", row["prob_draw"]), ("Result", "Away Win", row["prob_away"])],
        key=lambda x: x[2],
    )
    btts = row["btts_yes_prob"]
    btts_pick = ("BTTS", "Both Teams to Score", btts) if btts >= 0.5 else ("BTTS", "Not Both Teams to Score", 1 - btts)
    over = row["over_2_5_prob"]
    over_pick = ("Over/Under", "Over 2.5 Goals", over) if over >= 0.5 else ("Over/Under", "Under 2.5 Goals", 1 - over)
    return max([result, btts_pick, over_pick], key=lambda x: x[2])


def build_feature_row(fixture: pd.Series, engine, tracker, elo_config: EloConfig) -> dict:
    """Same features a training row would have, computed from live current state
    instead of mid-history - the tracker is read, never updated, since this fixture
    hasn't happened yet."""
    home, away, date, season = fixture["HomeTeam"], fixture["AwayTeam"], fixture["Date"], fixture["season"]
    elo_home, elo_away = engine.get(home), engine.get(away)
    elo_diff = elo_home + elo_config.home_advantage - elo_away
    feats = tracker.read(home, away, date, season, elo_home, elo_away)
    feats["elo_diff"] = elo_diff
    feats["abs_elo_diff"] = abs(elo_diff)
    feats["league"] = fixture["league"]
    return feats


def export_history(conn, all_matches: pd.DataFrame, state: dict) -> None:
    """Mirrors historical results and current Elo ratings into SQLite, for the
    team-profile and head-to-head pages - the website's website Node side has no
    other access to this, it only ever reads the shared database."""
    matches_export = all_matches[["league", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]].rename(
        columns={
            "Date": "date", "HomeTeam": "home_team", "AwayTeam": "away_team",
            "FTHG": "home_goals", "FTAG": "away_goals", "FTR": "result",
        }
    )
    matches_export["league"] = matches_export["league"].astype(str)
    matches_export["date"] = matches_export["date"].dt.strftime("%Y-%m-%d")
    storage.save_matches(conn, matches_export)

    ratings_export = pd.DataFrame(
        [
            {"league": league, "team": team, "elo_rating": rating}
            for league, (engine, _tracker) in state.items()
            for team, rating in engine.ratings.items()
        ]
    )
    storage.save_team_ratings(conn, ratings_export)


def main() -> None:
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")

    current_year = current_season_start_year()
    season_start_years = range(2017, current_year + 1)

    print(f"=== Loading history through the in-progress {current_year}/{current_year + 1 - 2000} season ===\n")
    raw = load_raw_matches(season_start_years, force_years={current_year})
    all_matches, state = build_dataset(elo_config=TUNED_ELO_CONFIG, raw=raw, return_state=True)
    all_matches["league"] = all_matches["league"].astype("category")
    print(f"\n{len(all_matches)} matches total.\n")

    conn = storage.connect()
    export_history(conn, all_matches, state)
    print("Historical matches and current Elo ratings exported for the website.\n")

    print("=== Fitting final models on all available history ===\n")
    y_train = all_matches["FTR"].map(OUTCOME_CODES)
    elo_clf = LogisticRegression(max_iter=1000).fit(all_matches[ELO_FEATURES], y_train)
    # gbm's tuned config relies on recency-weighted training (unlike elo, which was
    # never validated with any decay) - applying it here is what makes this fit match
    # what tune.py actually validated, instead of the setting being tuned and then
    # silently never used for real
    gbm_weights = decay_weights(all_matches["Date"], all_matches["Date"].max(), GBM_DECAY_HALF_LIFE_DAYS)
    gbm_clf = make_gbm().fit(all_matches[GBM_FEATURES], y_train, sample_weight=gbm_weights)
    poisson_models = fit_for_prediction(all_matches, half_life_days=POISSON_HALF_LIFE_DAYS, use_dixon_coles=False)

    print("=== Fetching upcoming fixtures ===\n")
    fixtures = fetch_fixtures()
    fixtures["season"] = current_year
    print(f"{len(fixtures)} upcoming fixtures found.\n")

    if fixtures.empty:
        print("Nothing to predict right now.")
    else:
        feature_rows = [
            build_feature_row(fx, *state[fx["league"]], TUNED_ELO_CONFIG) for _, fx in fixtures.iterrows()
        ]
        feature_df = pd.DataFrame(feature_rows, index=fixtures.index)
        feature_df["league"] = feature_df["league"].astype(all_matches["league"].dtype)

        elo_proba = pd.DataFrame(
            elo_clf.predict_proba(feature_df[ELO_FEATURES]), columns=OUTCOMES, index=feature_df.index
        )
        gbm_proba = pd.DataFrame(
            gbm_clf.predict_proba(feature_df[GBM_FEATURES]), columns=OUTCOMES, index=feature_df.index
        )
        poisson_markets = predict_fixtures(fixtures, poisson_models)

        ensemble = (elo_proba + gbm_proba + poisson_markets[OUTCOMES]) / 3
        ensemble = ensemble.div(ensemble.sum(axis=1), axis=0)

        output = fixtures.copy()
        output["prob_home"], output["prob_draw"], output["prob_away"] = ensemble["H"], ensemble["D"], ensemble["A"]
        output["predicted_outcome"] = ensemble.idxmax(axis=1)

        # goal-based markets come straight from the Poisson model - Elo and GBM predict the
        # discrete result only, they have no notion of expected goals to draw these from
        for col in [
            "expected_goals_home", "expected_goals_away", "correct_score_home", "correct_score_away",
            "btts_yes_prob", "over_1_5_prob", "over_2_5_prob", "over_3_5_prob",
            "double_chance_1x_prob", "double_chance_x2_prob", "double_chance_12_prob",
            "home_clean_sheet_prob", "away_clean_sheet_prob",
        ]:
            output[col] = poisson_markets[col]

        output["best_pick_market"], output["best_pick_label"], output["best_pick_prob"] = zip(
            *output.apply(best_pick, axis=1)
        )

        print("=== Predictions (ensemble of Elo, gradient boosting, and Poisson) ===\n")
        print(
            output[
                ["league", "Date", "HomeTeam", "AwayTeam", "prob_home", "prob_draw", "prob_away", "predicted_outcome",
                 "correct_score_home", "correct_score_away", "btts_yes_prob", "over_2_5_prob", "best_pick_label"]
            ].to_string(index=False)
        )

        to_store = output.rename(columns={"HomeTeam": "home_team", "AwayTeam": "away_team"})
        to_store["match_date"] = to_store["Date"].dt.strftime("%Y-%m-%d")
        n_new = storage.save_predictions(conn, to_store, predicted_at=dt.datetime.now().isoformat())
        print(f"\n{n_new} new predictions saved to {storage.DB_PATH}")

    # settling runs regardless of whether there were new fixtures to predict this time -
    # matches from an earlier run can finish even on a day with nothing new to forecast
    results_by_league = {
        code: raw[code][["Date", "HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"]].dropna() for code in LEAGUES
    }
    n_settled = storage.settle(conn, results_by_league)
    print(f"{n_settled} previously-logged predictions settled with real results.")

    board = storage.scoreboard(conn)
    if not board.empty:
        print("\n=== Track record so far ===\n")
        print(board.to_string(index=False))
    else:
        print("\nNo settled predictions yet - check back after this round of fixtures finishes.")
    conn.close()


if __name__ == "__main__":
    main()
