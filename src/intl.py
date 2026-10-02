"""National-team football: Elo ratings, a Poisson goals model on top of them, and
predictions for the upcoming internationals.

Why not reuse the club-league machinery (elo.py / features.py / the per-team Poisson GLM)?
That is built for a closed league - ~20 teams meeting each other twice a season, thousands
of shared-opponent matches. National teams are the opposite: 300+ sides, a handful of games
a year each, most opponents never faced, and a match's weight varies enormously (a World
Cup game vs a friendly). So this follows the World Football Elo approach - rating change
scaled by tournament importance and margin of victory - and turns the rating gap straight
into expected goals with one small regression, instead of per-team attack/defence terms
that would be unidentifiable on so little data. Every goal-based market then comes off the
same scoreline grid the club model uses (poisson_model.market_probs).

History: martj42/international_results (CC0, every international since 1872) - but it's
maintained by hand and lags by weeks, so the gap since its last row is filled from ESPN's
finished matches, and that gap shrinks by itself whenever the dataset catches up.

Usage (walk-forward validation of the settings below):
    python src/intl.py
"""

import datetime as dt
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import statsmodels.api as sm
from sklearn.metrics import accuracy_score, log_loss

from poisson_model import best_pick, market_probs

RESULTS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "intl_results.csv"

# eloratings.net's published constants - used as-is rather than tuned: international
# football has far too few matches per team for a search over these to beat them reliably.
HOME_ADVANTAGE = 100.0
INITIAL_RATING = 1500.0
HISTORY_START = "1995-01-01"  # Elo warm-up; anything older adds nothing a decade of games doesn't
GOAL_MODEL_START = "2006-01-01"
GOAL_MODEL_HALF_LIFE_YEARS = 8.0
ACTIVE_WITHIN_DAYS = 4 * 365  # a nation with no game in 4 years isn't in the "rank among nations"
MAX_BACKFILL_DAYS = 400  # past days are cached on disk after one fetch; this just stops a runaway first run

# ESPN's name -> the dataset's name, for the handful that differ (verified against 156
# distinct ESPN national-team names: every other name already matches verbatim).
ESPN_ALIASES = {
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Congo DR": "DR Congo",
    "Czechia": "Czech Republic",
    "Kyrgyz Republic": "Kyrgyzstan",
    "Sao Tome and Principe": "São Tomé and Príncipe",
    "St. Kitts and Nevis": "Saint Kitts and Nevis",
    "St. Lucia": "Saint Lucia",
    "St. Martin": "Saint Martin",
    "St. Vincent and the Grenadines": "Saint Vincent and the Grenadines",
    "Türkiye": "Turkey",
    "US Virgin Islands": "United States Virgin Islands",
}

# ESPN competition name -> the dataset's tournament name, so both sources weigh a match the same way.
ESPN_TOURNAMENT = {
    "International Friendly": "Friendly",
    "UEFA Nations League": "UEFA Nations League",
    "Concacaf Nations League": "CONCACAF Nations League",
    "UEFA Euro Qualifying": "UEFA Euro qualification",
    "FIFA World Cup": "FIFA World Cup",
    "UEFA European Championship": "UEFA Euro",
    "Copa America": "Copa América",
    "Africa Cup of Nations": "African Cup of Nations",
    "AFC Asian Cup": "AFC Asian Cup",
    "Concacaf Gold Cup": "Gold Cup",
}
for _confederation in ("UEFA", "CONMEBOL", "Concacaf", "CAF", "AFC", "OFC"):
    ESPN_TOURNAMENT[f"World Cup Qualifying ({_confederation})"] = "FIFA World Cup qualification"

# Finals tournaments are played at one host's venues: nobody but the host has a home crowd,
# and which side the feed calls "home" is arbitrary, so no home advantage is applied.
NEUTRAL_VENUE_TOURNAMENTS = {
    "FIFA World Cup", "UEFA Euro", "Copa América", "African Cup of Nations", "AFC Asian Cup", "Gold Cup",
    "Confederations Cup",
}
_CONTINENTAL_FINALS = {t.lower() for t in NEUTRAL_VENUE_TOURNAMENTS if t != "FIFA World Cup"} | {
    "concacaf championship", "oceania nations cup",
}


def tournament_weight(tournament: str) -> float:
    """eloratings.net's K by importance: World Cup finals 60, continental finals 50,
    qualifiers and the Nations Leagues 40, friendlies 20, everything else 30."""
    t = tournament.lower()
    if t == "friendly":
        return 20.0
    if t == "fifa world cup":
        return 60.0
    if t in _CONTINENTAL_FINALS:
        return 50.0
    if "qualification" in t or t in ("uefa nations league", "concacaf nations league"):
        return 40.0
    return 30.0


def load_history(max_age_hours: float = 12) -> pd.DataFrame:
    """The open results dataset, re-downloaded at most every `max_age_hours`. If the
    download fails, a stale copy is used rather than losing the whole national-team side."""
    fresh = RESULTS_PATH.exists() and (
        dt.datetime.now().timestamp() - RESULTS_PATH.stat().st_mtime < max_age_hours * 3600
    )
    if not fresh:
        try:
            resp = requests.get(RESULTS_URL, timeout=60)
            resp.raise_for_status()
            RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULTS_PATH.write_bytes(resp.content)
        except requests.RequestException as exc:
            if not RESULTS_PATH.exists():
                raise
            print(f"  (couldn't refresh the international results dataset - using the cached copy: {exc})")

    df = pd.read_csv(RESULTS_PATH, parse_dates=["date"], encoding="utf-8")
    df = df.dropna(subset=["home_score", "away_score"])
    df = df[df["date"] >= HISTORY_START].copy()
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df["neutral"] = df["neutral"].astype(str).str.upper().eq("TRUE")
    return (
        df.rename(columns={"home_score": "home_goals", "away_score": "away_goals"})
        [["date", "home_team", "away_team", "home_goals", "away_goals", "tournament", "neutral"]]
        .sort_values("date", kind="stable")
        .reset_index(drop=True)
    )


def canonical(name: str) -> str:
    return ESPN_ALIASES.get(name, name)


def espn_window_start(history: pd.DataFrame, today: dt.date, past_days: int = 10) -> dt.date:
    """First day of ESPN data needed: back to the day after the dataset's last row (to
    backfill ratings), and at least `past_days` (to settle recently-finished predictions)."""
    after_history = history["date"].max().date() + dt.timedelta(days=1)
    earliest = today - dt.timedelta(days=MAX_BACKFILL_DAYS)
    if after_history < earliest:
        print(
            f"  (the international results dataset ends {after_history - dt.timedelta(days=1)}, over {MAX_BACKFILL_DAYS} days ago - "
            "results since then can't all be backfilled, so ratings will lag until it's updated)"
        )
    return max(min(after_history, today - dt.timedelta(days=past_days)), earliest)


def espn_results(soccer: pd.DataFrame) -> pd.DataFrame:
    """ESPN's finished internationals in the dataset's own shape. Only plain full-time
    results: a match that went to extra time or penalties has a different 'result' in the
    1X2 sense depending on who you ask, so those are left out rather than guessed at."""
    if soccer.empty:  # ESPN unreachable: no backfill, and the ratings simply stop at the dataset's last row
        return pd.DataFrame(columns=["external_id", "date", "home_team", "away_team", "home_goals", "away_goals", "tournament", "neutral"])
    done = soccer[(soccer["kind"] == "intl") & (soccer["state"] == "post") & (soccer["status"] == "STATUS_FULL_TIME")]
    done = done.dropna(subset=["home_goals", "away_goals"])
    tournament = done["competition"].map(ESPN_TOURNAMENT).fillna("Other")
    return pd.DataFrame(
        {
            "external_id": done["external_id"].to_numpy(),
            "date": done["kickoff_utc"].dt.tz_convert(None).dt.normalize().to_numpy(),
            "home_team": done["home"].map(canonical).to_numpy(),
            "away_team": done["away"].map(canonical).to_numpy(),
            "home_goals": done["home_goals"].astype(int).to_numpy(),
            "away_goals": done["away_goals"].astype(int).to_numpy(),
            "tournament": tournament.to_numpy(),
            "neutral": tournament.isin(NEUTRAL_VENUE_TOURNAMENTS).to_numpy(),
        }
    )


def combine_history(history: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Dataset rows plus any ESPN results newer than its last row (never both - the
    dataset's own copy of a match wins once it has caught up)."""
    newer = results[results["date"] > history["date"].max()].drop(columns="external_id")
    if newer.empty:
        return history
    return pd.concat([history, newer], ignore_index=True).sort_values("date", kind="stable").reset_index(drop=True)


def rate_matches(matches: pd.DataFrame):
    """Chronological Elo pass. Returns (matches + each side's PRE-match rating, final
    ratings, each team's last five results oldest-first). A match is rated before it
    updates anything, so the pre-match columns never leak that match's own result."""
    ratings: dict = defaultdict(lambda: INITIAL_RATING)
    form: dict = defaultdict(lambda: deque(maxlen=5))
    elo_home, elo_away = [], []

    for row in matches.itertuples(index=False):
        rh, ra = ratings[row.home_team], ratings[row.away_team]
        elo_home.append(rh)
        elo_away.append(ra)

        diff = rh - ra + (0.0 if row.neutral else HOME_ADVANTAGE)
        expected = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        margin = abs(row.home_goals - row.away_goals)
        if row.home_goals > row.away_goals:
            actual, home_res, away_res = 1.0, "W", "L"
        elif row.home_goals < row.away_goals:
            actual, home_res, away_res = 0.0, "L", "W"
        else:
            actual, home_res, away_res = 0.5, "D", "D"
        goal_factor = 1.0 if margin <= 1 else 1.5 if margin == 2 else (11 + margin) / 8
        delta = tournament_weight(row.tournament) * goal_factor * (actual - expected)
        ratings[row.home_team] = rh + delta
        ratings[row.away_team] = ra - delta
        form[row.home_team].append(home_res)
        form[row.away_team].append(away_res)

    rated = matches.copy()
    rated["elo_home"] = elo_home
    rated["elo_away"] = elo_away
    return rated, dict(ratings), {team: "".join(results) for team, results in form.items()}


def _design(elo_home, elo_away, neutral) -> pd.DataFrame:
    """Home-side features: rating gap (home advantage included, in units of 400 Elo points)
    and the pair's average strength (strong-vs-strong games score differently from
    weak-vs-weak ones at the same gap). The away side uses the negated gap."""
    gap = (elo_home - elo_away + np.where(neutral, 0.0, HOME_ADVANTAGE)) / 400.0
    level = ((elo_home + elo_away) / 2.0 - INITIAL_RATING) / 400.0
    return pd.DataFrame({"gap": gap, "level": level})


def fit_goal_model(rated: pd.DataFrame, as_of: pd.Timestamp = None) -> np.ndarray:
    """Poisson regression, goals ~ 1 + gap + level, on both sides' goals pooled (the home
    advantage is already inside `gap`, so one symmetric model serves both). Recent matches
    count more - the game's tempo and scoring have drifted over the years."""
    train = rated[rated["date"] >= GOAL_MODEL_START]
    if as_of is not None:
        train = train[train["date"] < as_of]
    design = _design(train["elo_home"].to_numpy(), train["elo_away"].to_numpy(), train["neutral"].to_numpy())

    home_rows = design.assign(goals=train["home_goals"].to_numpy())
    away_rows = design.assign(gap=-design["gap"], goals=train["away_goals"].to_numpy())
    long = pd.concat([home_rows, away_rows], ignore_index=True)

    ref = as_of if as_of is not None else train["date"].max()
    age_years = ((ref - train["date"]).dt.days / 365.25).to_numpy()
    weights = np.tile(0.5 ** (age_years / GOAL_MODEL_HALF_LIFE_YEARS), 2)

    model = sm.GLM(
        long["goals"], sm.add_constant(long[["gap", "level"]]), family=sm.families.Poisson(), var_weights=weights
    ).fit()
    return model.params.to_numpy()


def expected_goals(coef: np.ndarray, elo_home, elo_away, neutral) -> tuple:
    design = _design(np.asarray(elo_home, dtype=float), np.asarray(elo_away, dtype=float), np.asarray(neutral))
    base = coef[0] + coef[2] * design["level"].to_numpy()
    return np.exp(base + coef[1] * design["gap"].to_numpy()), np.exp(base - coef[1] * design["gap"].to_numpy())


def predict_fixtures(fixtures: pd.DataFrame, ratings: dict, form: dict, coef: np.ndarray) -> pd.DataFrame:
    """fixtures: ESPN rows (home/away already canonical) for matches not yet played.
    Teams the dataset has never seen are skipped, not guessed at - an invented rating
    would make a confident-looking prediction out of nothing."""
    known = fixtures[fixtures["home"].isin(ratings) & fixtures["away"].isin(ratings)]
    unknown = sorted(set(fixtures["home"]).union(fixtures["away"]) - set(ratings))
    if unknown:
        print(f"  (no rating history for {len(unknown)} national team(s), skipping their fixtures: {', '.join(unknown)})")
    if known.empty:
        return pd.DataFrame()

    neutral = known["competition_dataset"].isin(NEUTRAL_VENUE_TOURNAMENTS).to_numpy()
    elo_h = known["home"].map(ratings).to_numpy()
    elo_a = known["away"].map(ratings).to_numpy()
    lam_h, lam_a = expected_goals(coef, elo_h, elo_a, neutral)

    rows = []
    for (_, fx), lh, la, rh, ra in zip(known.iterrows(), lam_h, lam_a, elo_h, elo_a):
        m = market_probs(float(lh), float(la))
        row = {
            "sport": "football",
            "league": "INT",
            "competition": fx["competition"],
            "external_id": fx["external_id"],
            "kickoff_utc": fx["kickoff_utc"],
            "match_date": fx["kickoff_utc"].strftime("%Y-%m-%d"),
            "home_team": fx["home"],
            "away_team": fx["away"],
            "home_logo": fx["home_logo"],
            "away_logo": fx["away_logo"],
            "prob_home": m["H"], "prob_draw": m["D"], "prob_away": m["A"],
            "home_form": form.get(fx["home"], ""),
            "away_form": form.get(fx["away"], ""),
            "home_rating": round(rh),
            "away_rating": round(ra),
        }
        row["predicted_outcome"] = max(("H", "D", "A"), key=lambda k: row[{"H": "prob_home", "D": "prob_draw", "A": "prob_away"}[k]])
        for col in (
            "expected_goals_home", "expected_goals_away", "correct_score_home", "correct_score_away",
            "btts_yes_prob", "over_1_5_prob", "over_2_5_prob", "over_3_5_prob",
            "double_chance_1x_prob", "double_chance_x2_prob", "double_chance_12_prob",
            "home_clean_sheet_prob", "away_clean_sheet_prob",
        ):
            row[col] = m[col]
        row["best_pick_market"], row["best_pick_label"], row["best_pick_prob"] = best_pick(row)
        rows.append(row)
    return pd.DataFrame(rows)


def build(history: pd.DataFrame, soccer: pd.DataFrame, today: dt.date = None, horizon_days: int = 10) -> dict:
    """Everything the national-team side contributes to one prediction run:
      predictions - upcoming fixtures, ready for storage.save_predictions
      results     - finished ESPN internationals (for settling earlier predictions)
      matches     - match history for the head-to-head / team-profile pages
      ratings     - current Elo per active nation, for the team-profile page
    `soccer` is espn.fetch_soccer's frame - passed in so the club-league side can share
    the same download instead of fetching it twice."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    results = espn_results(soccer)
    matches = combine_history(history, results)
    rated, ratings, form = rate_matches(matches)
    coef = fit_goal_model(rated)
    print(
        f"National teams: {len(matches)} matches rated ({len(matches) - len(history)} of them backfilled from ESPN), "
        f"{len(ratings)} teams; goal model gap={coef[1]:.3f} level={coef[2]:.3f}"
    )

    now = pd.Timestamp.now(tz="UTC")
    upcoming = soccer[
        (soccer["kind"] == "intl")
        & (soccer["state"] == "pre")
        & (soccer["kickoff_utc"] >= now)
        & (soccer["kickoff_utc"] < now + pd.Timedelta(days=horizon_days))
    ].copy()
    upcoming["home"] = upcoming["home"].map(canonical)
    upcoming["away"] = upcoming["away"].map(canonical)
    upcoming["competition_dataset"] = upcoming["competition"].map(ESPN_TOURNAMENT).fillna("Other")
    predictions = predict_fixtures(upcoming, ratings, form, coef) if len(upcoming) else pd.DataFrame()

    cutoff = matches["date"].max() - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)
    recent = matches[matches["date"] >= cutoff]
    active = set(recent["home_team"]) | set(recent["away_team"])
    ratings_export = pd.DataFrame(
        [{"league": "INT", "team": team, "elo_rating": rating} for team, rating in ratings.items() if team in active]
    )

    export = matches.assign(
        league="INT",
        date=matches["date"].dt.strftime("%Y-%m-%d"),
        result=np.select(
            [matches["home_goals"] > matches["away_goals"], matches["home_goals"] < matches["away_goals"]], ["H", "A"], "D"
        ),
    )[["league", "date", "home_team", "away_team", "home_goals", "away_goals", "result"]]
    return {"predictions": predictions, "results": results, "matches": export, "ratings": ratings_export}


def evaluate(first_test_year: int = 2022) -> None:
    """Walk-forward check of the goal model: for each calendar year from `first_test_year`,
    fit on matches strictly before it and score that year's matches - against a baseline
    that just predicts the training set's H/D/A frequencies."""
    history = load_history()
    rated, _, _ = rate_matches(history)
    labels = ["A", "D", "H"]
    outcome = np.select([rated["home_goals"] > rated["away_goals"], rated["home_goals"] < rated["away_goals"]], ["H", "A"], "D")
    rated = rated.assign(outcome=outcome)

    print(f"{'year':<6}{'matches':>8}{'accuracy':>10}{'log-loss':>10}{'baseline acc':>14}{'baseline ll':>13}")
    for year in range(first_test_year, history["date"].max().year + 1):
        start = pd.Timestamp(f"{year}-01-01")
        train, test = rated[rated["date"] < start], rated[(rated["date"] >= start) & (rated["date"] < start + pd.DateOffset(years=1))]
        if test.empty:
            continue
        coef = fit_goal_model(rated, as_of=start)
        lam_h, lam_a = expected_goals(coef, test["elo_home"], test["elo_away"], test["neutral"])
        proba = np.array([[market_probs(h, a)[k] for k in labels] for h, a in zip(lam_h, lam_a)])
        freq = train["outcome"].value_counts(normalize=True).reindex(labels).to_numpy()
        base = np.tile(freq, (len(test), 1))
        print(
            f"{year:<6}{len(test):>8}{accuracy_score(test['outcome'], np.array(labels)[proba.argmax(axis=1)]):>10.3f}"
            f"{log_loss(test['outcome'], proba, labels=labels):>10.3f}"
            f"{accuracy_score(test['outcome'], np.array(labels)[base.argmax(axis=1)]):>14.3f}"
            f"{log_loss(test['outcome'], base, labels=labels):>13.3f}"
        )


if __name__ == "__main__":
    evaluate()
