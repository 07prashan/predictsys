"""Predict upcoming matches across everything this site covers - club football in 17
European divisions, national-team football, and ATP/WTA tennis - log those predictions,
and settle any earlier predictions whose matches have since been played.

This is the one script meant to run on a schedule - each run: refreshes the
in-progress season's results, settles anything that finished since last time,
predicts what's coming up next, and stores those predictions for grading
once they, too, are settled on a future run.

Each sport runs in isolation: a feed that's down (or a model that throws) is reported and
skipped, and everything else still runs and saves. The exit code is non-zero if any part
failed, so a scheduler's log still shows it.

Usage:
    python src/predict.py                        # everything
    python src/predict.py --only intl tennis     # skip the (slow) club-league refit
"""

import argparse
import datetime as dt
import sys
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression

import espn
import intl
import storage
import tennis
from backtest import OUTCOME_CODES, OUTCOMES, decay_weights
from elo import EloConfig
from fetch_data import LEAGUES, current_season_start_year
from fetch_fixtures import fetch_fixtures
from poisson_model import best_pick, fit_for_prediction, predict_fixtures
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
from team_match import match_clubs

# How far ahead matches are predicted and shown. The site promises a full week of days, so
# this is comfortably more than that - the extra days are what a Saturday-to-Saturday
# lookahead needs, and still near enough that the ratings behind a prediction aren't stale.
HORIZON_DAYS = 10
SPORTS = ("clubs", "intl", "tennis")


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


def recent_form(matches: pd.DataFrame, games: int = 5) -> dict:
    """{team: 'WDLWW'} - each team's last `games` league results, oldest first.
    `matches` must be in date order (load_league sorts it that way)."""
    results = defaultdict(list)
    for home, away, ftr in matches[["HomeTeam", "AwayTeam", "FTR"]].itertuples(index=False):
        home_res, away_res = {"H": ("W", "L"), "A": ("L", "W")}.get(ftr, ("D", "D"))
        results[home].append(home_res)
        results[away].append(away_res)
    return {team: "".join(r[-games:]) for team, r in results.items()}


def espn_club_fixtures(soccer: pd.DataFrame, raw: dict) -> pd.DataFrame:
    """Upcoming club fixtures from ESPN, with each team renamed to football-data's spelling
    (what the models are keyed on). A fixture whose teams can't be resolved is skipped and
    reported - predicting it would mean guessing which team ESPN meant."""
    now = pd.Timestamp.now(tz="UTC")
    upcoming = soccer[
        (soccer["kind"] == "club")
        & (soccer["state"] == "pre")
        & (soccer["kickoff_utc"] >= now)
        & (soccer["kickoff_utc"] < now + pd.Timedelta(days=HORIZON_DAYS))
    ]
    frames, skipped = [], []
    for code, group in upcoming.groupby("league_code"):
        league = raw[code]
        this_season = league[league["season"] == league["season"].max()]
        current = set(this_season["HomeTeam"]) | set(this_season["AwayTeam"])
        resolved, unresolved = match_clubs(
            set(group["home"]) | set(group["away"]), current, set(league["HomeTeam"]) | set(league["AwayTeam"])
        )
        skipped += [f"{code}: {name}" for name in unresolved]
        keep = group[group["home"].isin(resolved) & group["away"].isin(resolved)]
        form = recent_form(league)
        home, away = keep["home"].map(resolved), keep["away"].map(resolved)
        frames.append(
            pd.DataFrame(
                {
                    "league": code,
                    "Date": keep["kickoff_utc"].dt.tz_convert(None).dt.normalize(),
                    "HomeTeam": home,
                    "AwayTeam": away,
                    "kickoff_utc": keep["kickoff_utc"],
                    "external_id": keep["external_id"],
                    "home_logo": keep["home_logo"],
                    "away_logo": keep["away_logo"],
                    "home_form": home.map(form),
                    "away_form": away.map(form),
                }
            )
        )
    if skipped:
        print(f"  (couldn't match {len(skipped)} ESPN club name(s) to the models' teams, skipping: {', '.join(skipped)})")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def upcoming_club_fixtures(soccer: pd.DataFrame, raw: dict) -> pd.DataFrame:
    """ESPN's schedule first (it looks well ahead and keeps listing fixtures through an
    international break), football-data's next-round feed for anything ESPN lacks."""
    espn_rows = espn_club_fixtures(soccer, raw)
    try:
        fallback = fetch_fixtures()
    except requests.RequestException as exc:
        print(f"  (football-data fixtures unavailable - using ESPN's schedule alone: {exc})")
        fallback = pd.DataFrame()

    if not fallback.empty:
        started = fallback["kickoff_utc"].notna() & (fallback["kickoff_utc"] < pd.Timestamp.now(tz="UTC"))
        fallback = fallback[~started]
    combined = pd.concat([espn_rows, fallback], ignore_index=True)
    # the same fixture can arrive from both feeds; ESPN's copy comes first and wins
    return combined.drop_duplicates(["league", "HomeTeam", "AwayTeam"], keep="first").reset_index(drop=True)


def espn_settlements(soccer: pd.DataFrame) -> pd.DataFrame:
    """Finished football matches as (external_id, outcome, home_score, away_score). Only plain
    full-time results - see intl.espn_results for why extra time and penalties are left out."""
    done = soccer[(soccer["state"] == "post") & (soccer["status"] == "STATUS_FULL_TIME")].dropna(
        subset=["home_goals", "away_goals"]
    )
    outcome = np.select([done["home_goals"] > done["away_goals"], done["home_goals"] < done["away_goals"]], ["H", "A"], "D")
    return pd.DataFrame(
        {
            "external_id": done["external_id"].to_numpy(),
            "outcome": outcome,
            "home_score": done["home_goals"].astype(int).to_numpy(),
            "away_score": done["away_goals"].astype(int).to_numpy(),
        }
    )


def tennis_settlements(results: pd.DataFrame) -> pd.DataFrame:
    done = results[(results["winner"] > 0) & ~results["status"].str.contains("WALKOVER", case=False)]
    return pd.DataFrame(
        {
            "external_id": done["external_id"].to_numpy(),
            "outcome": np.where(done["winner"] == 1, "H", "A"),
            "home_score": done["p1_sets"].astype(int).to_numpy(),
            "away_score": done["p2_sets"].astype(int).to_numpy(),
        }
    )


def run_clubs(conn, soccer: pd.DataFrame) -> None:
    current_year = current_season_start_year()
    season_start_years = range(2017, current_year + 1)

    print(f"=== Loading history through the in-progress {current_year}/{current_year + 1 - 2000} season ===\n")
    raw = load_raw_matches(season_start_years, force_years={current_year})
    all_matches, state = build_dataset(elo_config=TUNED_ELO_CONFIG, raw=raw, return_state=True)
    all_matches["league"] = all_matches["league"].astype("category")
    print(f"\n{len(all_matches)} matches total.\n")

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
    fixtures = upcoming_club_fixtures(soccer, raw)
    print(f"{len(fixtures)} upcoming fixtures found.\n")

    if fixtures.empty:
        print("Nothing to predict right now.")
    else:
        fixtures["season"] = current_year
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
        output["home_rating"] = [round(state[lg][0].get(t)) for lg, t in zip(output["league"], output["HomeTeam"])]
        output["away_rating"] = [round(state[lg][0].get(t)) for lg, t in zip(output["league"], output["AwayTeam"])]

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
        print(f"\n{n_new} new club predictions saved to {storage.DB_PATH}")

    # settling runs regardless of whether there were new fixtures to predict this time -
    # matches from an earlier run can finish even on a day with nothing new to forecast
    results_by_league = {
        code: raw[code][["Date", "HomeTeam", "AwayTeam", "FTR", "FTHG", "FTAG"]].dropna() for code in LEAGUES
    }
    n_settled = storage.settle(conn, results_by_league)
    print(f"{n_settled} previously-logged club predictions settled with real results.")


def run_national_teams(conn, history: pd.DataFrame, soccer: pd.DataFrame, today: dt.date) -> None:
    print("\n=== National teams ===\n")
    out = intl.build(history, soccer, today, HORIZON_DAYS)
    if len(out["predictions"]):
        print(
            out["predictions"][["kickoff_utc", "competition", "home_team", "away_team", "prob_home", "prob_draw", "prob_away", "best_pick_label"]]
            .to_string(index=False)
        )
    n_new = storage.save_predictions(conn, out["predictions"], predicted_at=dt.datetime.now().isoformat()) if len(out["predictions"]) else 0
    print(f"\n{n_new} new national-team predictions saved.")
    storage.save_matches(conn, out["matches"])
    storage.save_team_ratings(conn, out["ratings"])


def run_tennis(conn, today: dt.date) -> pd.DataFrame:
    print("\n=== Tennis ===\n")
    out = tennis.build(today, HORIZON_DAYS)
    if len(out["predictions"]):
        print(
            out["predictions"][["kickoff_utc", "league", "competition", "round", "home_team", "away_team", "prob_home", "best_pick_label"]]
            .to_string(index=False)
        )
    n_new = storage.save_predictions(conn, out["predictions"], predicted_at=dt.datetime.now().isoformat()) if len(out["predictions"]) else 0
    print(f"\n{n_new} new tennis predictions saved.")
    storage.save_matches(conn, out["matches"])
    storage.save_team_ratings(conn, out["ratings"])
    return tennis_settlements(out["results"])


def main(only: list = None) -> int:
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 40)

    sports = set(only or SPORTS)
    today = dt.datetime.now(dt.timezone.utc).date()
    conn = storage.connect()
    failures = []

    def attempt(name, step):
        try:
            return step()
        except Exception:  # noqa: BLE001 - one sport's failure must not cost the others their run
            failures.append(name)
            print(f"\n!!! {name} failed - continuing with the rest:\n")
            traceback.print_exc()
            return None

    # one ESPN football download is shared by the club and national-team sides
    history = attempt("national-team history", intl.load_history) if "intl" in sports else None
    soccer = pd.DataFrame(columns=espn.SOCCER_COLUMNS)
    if sports & {"clubs", "intl"}:
        start = intl.espn_window_start(history, today) if history is not None else today - dt.timedelta(days=10)
        print("=== Fetching football schedule and results from ESPN ===\n")
        soccer = espn.fetch_soccer(start, today + dt.timedelta(days=HORIZON_DAYS + 1))
        print(f"{len(soccer)} ESPN football matches ({start} to {today + dt.timedelta(days=HORIZON_DAYS + 1)}).")

    settlements = []
    if "intl" in sports and history is not None:
        attempt("national teams", lambda: run_national_teams(conn, history, soccer, today))
    if "tennis" in sports:
        tennis_done = attempt("tennis", lambda: run_tennis(conn, today))
        if tennis_done is not None:
            settlements.append(tennis_done)
    if "clubs" in sports:
        attempt("club leagues", lambda: run_clubs(conn, soccer))

    # ESPN's finished football and tennis matches settle any stored prediction carrying the
    # same ESPN id - fast (no waiting for football-data's results files) and name-independent
    if len(soccer):
        settlements.append(espn_settlements(soccer))
    if settlements:
        n = storage.settle_by_external_id(conn, pd.concat(settlements, ignore_index=True))
        print(f"\n{n} predictions settled from ESPN results.")

    board = storage.scoreboard(conn)
    if not board.empty:
        print("\n=== Track record so far ===\n")
        print(board.to_string(index=False))
    else:
        print("\nNo settled predictions yet - check back after this round of fixtures finishes.")
    conn.close()

    if failures:
        print(f"\nFinished with failures in: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=SPORTS, help="run just these parts (default: all)")
    sys.exit(main(parser.parse_args().only))
