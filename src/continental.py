"""Cross-border club football: one global club rating, and predictions for every
continental competition (UCL, UEL, Conference League, Libertadores, ACL Elite, ...).

The per-league machinery (elo.py + features.py + the per-team Poisson GLM) is built for a
closed league: ~20 clubs meeting each other twice a season, thousands of shared-opponent
matches, one attack/defense term per club. A continental tie is the opposite situation -
Real Madrid and Al Hilal may never have a single shared opponent, and half the field comes
from leagues this site doesn't model at all. So this module follows the national-team
design in intl.py instead: one Elo rating per club, a match's weight scaled by how much it
says about strength, and the rating gap turned into expected goals by a small Poisson
regression - but on a GLOBAL pool of matches rather than internationals:

  - every covered domestic league's results (from the same football-data history the club
    models use) order clubs inside their own league, and
  - continental results are the only matches linking leagues to each other, so they are
    what calibrates league against league on the shared scale.

That means a Saudi or Qatari club - never modeled domestically here - still earns a real
rating from its continental results alone, and a European club's rating carries its league's
strength into the tie. Club names are resolved to football-data's spelling wherever a club
belongs to a covered league (so its domestic results anchor its rating); anything
unresolvable keeps ESPN's spelling and is rated purely off continental play rather than
skipped - a debuting club with no domestic anchor is still better priced off its own
continental results than not at all.

History: continental seasons are backfilled from ESPN one competition-year per request
(espn.fetch_league_history, cached on disk), which costs ~110 requests once and a handful
per run afterwards.

Usage (walk-forward-style sanity check of the settings below):
    python src/continental.py
"""

import datetime as dt
from collections import defaultdict, deque

import numpy as np
import pandas as pd
import statsmodels.api as sm

import espn
from fetch_data import ALL_LEAGUES
from poisson_model import best_pick, market_probs
from team_match import match_club

# The domestic leagues' history starts in 2017 (see fetch_data.WORLD_FIRST_SEASON) - the
# continental backfill starts there too, so league-to-league calibration has the same
# window as the within-league orderings it sits on.
HISTORY_START_YEAR = 2017

INITIAL_RATING = 1500.0
# Elo K by match kind. Domestic results are the bulk of the data and mostly re-state what a
# league's own table already says; a continental result is rarer and is the ONLY evidence
# linking two leagues, so each one moves ratings roughly twice as much. Same margin-of-
# victory scaling as intl.py - a 4-0 says more than a 1-0 at any K.
K_DOMESTIC = 20.0
K_CONTINENTAL = 40.0
# Home edge, in Elo points. Domestic matches use the tuned club value (run.TUNED_ELO_CONFIG
# has 50). Continental ties are shrunk below that: longer travel, rotated sides in dead-rubber
# league-phase rounds, and neutral-venue one-off finals all eat into the usual home edge.
HOME_ADVANTAGE = 50.0
CONTINENTAL_HOME_ADVANTAGE = 30.0

# Expected goals from the rating gap: one Poisson regression, goals ~ gap + level, fit on
# every match (the level term keeps strong-vs-strong from scoring like weak-vs-weak at the
# same gap - the same two features intl.py validated on internationals). Recent matches
# count more; two years matches the club model's tuned Poisson half-life.
GOAL_MODEL_START = "2015-01-01"
GOAL_MODEL_HALF_LIFE_YEARS = 2.0

# How many seasons of continental results back each club's page (head-to-head and recent
# form on the team profile). The rating itself always uses the full history.
PAGE_HISTORY_SEASONS = 4


def domestic_matches(raw: dict) -> pd.DataFrame:
    """Every covered domestic league's results on the global pool's own shape:
    date, home, away, home_goals, away_goals, kind='domestic', competition=league code.
    `raw` is run.load_raw_matches's dict (league code -> frame)."""
    frames = []
    for code, df in raw.items():
        if df is None or not len(df) or "FTHG" not in df.columns:
            continue
        done = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        if not len(done):
            continue
        frames.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(done["Date"]).dt.tz_localize(None).dt.normalize(),
                    "home": done["HomeTeam"].astype(str),
                    "away": done["AwayTeam"].astype(str),
                    "home_goals": done["FTHG"].astype(int),
                    "away_goals": done["FTAG"].astype(int),
                    "kind": "domestic",
                    "competition": code,
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["date", "home", "away", "home_goals", "away_goals", "kind", "competition"])
    return pd.concat(frames, ignore_index=True).sort_values("date", kind="stable").reset_index(drop=True)


def continental_history(soccer: pd.DataFrame, first_year: int = None, this_year: int = None) -> pd.DataFrame:
    """Finished continental matches since `first_year`, as the global pool's shape plus
    `competition` (our league code). `soccer` is a daily-feed frame - passed in only so its
    already-fetched current-year window can be reused instead of re-requested; every past
    season comes from the per-competition year fetch (cached on disk after the first run)."""
    this_year = this_year or dt.datetime.now(dt.timezone.utc).year
    first_year = first_year or HISTORY_START_YEAR

    frames = []
    for code, slug in espn.CONTINENTAL_SLUGS.items():
        for year in range(first_year, this_year):
            df = espn.fetch_league_history(code, slug, year, final=True)
            if len(df):
                frames.append(df.assign(competition=code))
        current = espn.fetch_league_history(code, slug, this_year, final=False)
        if len(current):
            frames.append(current.assign(competition=code))

    done = _finished(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    if not len(done):
        return pd.DataFrame(columns=["date", "home", "away", "home_goals", "away_goals", "kind", "competition"])
    # the daily feed's copy of a match already in a year fetch would only duplicate it
    done = done.drop_duplicates(["competition", "kickoff_utc", "home", "away"])
    if len(soccer):
        from_daily = _finished(soccer[soccer["kind"] == "continental"])
        if len(from_daily):
            # the daily frame carries no competition code - league_code is it for those rows
            from_daily = from_daily.assign(competition=from_daily["league_code"])
            done = pd.concat([done, from_daily], ignore_index=True).drop_duplicates(
                ["competition", "kickoff_utc", "home", "away"]
            )
    out = pd.DataFrame(
        {
            "date": done["kickoff_utc"].dt.tz_convert(None).dt.normalize(),
            "home": done["home"].astype(str),
            "away": done["away"].astype(str),
            "home_goals": done["home_goals"].astype(int),
            "away_goals": done["away_goals"].astype(int),
            "kind": "continental",
            "competition": done["competition"].astype(str),
        }
    )
    return out.sort_values("date", kind="stable").reset_index(drop=True)


def _finished(df: pd.DataFrame) -> pd.DataFrame:
    """Plain full-time results only - a tie that went to extra time or penalties has no
    single honest 1X2 outcome, so it's left out rather than guessed at (the same rule the
    national-team side follows)."""
    if not len(df):
        return df
    done = df[(df["state"] == "post") & (df["status"] == "STATUS_FULL_TIME")].dropna(
        subset=["home_goals", "away_goals"]
    )
    return done


# ---- ratings -----------------------------------------------------------------------


def rate_matches(matches: pd.DataFrame):
    """One chronological Elo pass over the whole global pool. Returns (matches + each side's
    PRE-match rating, final ratings, each club's last five results oldest-first). A match is
    rated before it updates anything, so the pre-match columns never leak its own result."""
    ratings: dict = {}
    form_store = defaultdict(lambda: deque(maxlen=5))
    elo_home, elo_away = [], []

    for row in matches.itertuples(index=False):
        rh, ra = ratings.get(row.home, INITIAL_RATING), ratings.get(row.away, INITIAL_RATING)
        elo_home.append(rh)
        elo_away.append(ra)

        continental = row.kind == "continental"
        k = K_CONTINENTAL if continental else K_DOMESTIC
        ha = CONTINENTAL_HOME_ADVANTAGE if continental else HOME_ADVANTAGE
        diff = rh - ra + ha
        expected = 1.0 / (1.0 + 10 ** (-diff / 400.0))
        margin = abs(row.home_goals - row.away_goals)
        if row.home_goals > row.away_goals:
            actual, home_res, away_res = 1.0, "W", "L"
        elif row.home_goals < row.away_goals:
            actual, home_res, away_res = 0.0, "L", "W"
        else:
            actual, home_res, away_res = 0.5, "D", "D"
        goal_factor = 1.0 if margin <= 1 else 1.5 if margin == 2 else (11 + margin) / 8
        delta = k * goal_factor * (actual - expected)
        ratings[row.home], ratings[row.away] = rh + delta, ra - delta
        form_store[row.home].append(home_res)
        form_store[row.away].append(away_res)

    rated = matches.copy()
    rated["elo_home"] = elo_home
    rated["elo_away"] = elo_away
    rated["continental"] = rated["kind"] == "continental"
    return rated, ratings, {team: "".join(results) for team, results in form_store.items()}


def _design(elo_home, elo_away, continental) -> pd.DataFrame:
    """Home-side features: rating gap (that side's own home advantage already included, in
    units of 400 points) and the pair's average strength. Continental rows carry the smaller
    continental home edge rather than the domestic one."""
    ha = np.where(continental, CONTINENTAL_HOME_ADVANTAGE, HOME_ADVANTAGE)
    gap = (np.asarray(elo_home, float) - np.asarray(elo_away, float) + ha) / 400.0
    level = ((np.asarray(elo_home, float) + np.asarray(elo_away, float)) / 2.0 - INITIAL_RATING) / 400.0
    return pd.DataFrame({"gap": gap, "level": level})


def fit_goal_model(rated: pd.DataFrame, as_of: pd.Timestamp = None) -> np.ndarray:
    """Poisson regression, goals ~ 1 + gap + level, on both sides' goals pooled across the
    whole global pool. Recent matches count more (the game's tempo and scoring drift)."""
    train = rated[(rated["date"] >= GOAL_MODEL_START)]
    if as_of is not None:
        train = train[train["date"] < as_of]
    design = _design(train["elo_home"], train["elo_away"], train["continental"])

    long = pd.concat(
        [
            design.assign(goals=train["home_goals"].to_numpy()),
            design.assign(gap=-design["gap"], goals=train["away_goals"].to_numpy()),
        ],
        ignore_index=True,
    )
    ref = as_of if as_of is not None else train["date"].max()
    age_years = ((ref - train["date"]).dt.days / 365.25).to_numpy()
    weights = np.tile(0.5 ** (age_years / GOAL_MODEL_HALF_LIFE_YEARS), 2)

    model = sm.GLM(
        long["goals"], sm.add_constant(long[["gap", "level"]]), family=sm.families.Poisson(), var_weights=weights
    ).fit()
    return model.params.to_numpy()


def expected_goals(coef: np.ndarray, elo_home, elo_away) -> tuple:
    """(home goals, away goals). Takes scalars or arrays; scalar in, scalar out, so a single
    hypothetical fixture doesn't have to unwrap a one-element array."""
    home = np.atleast_1d(np.asarray(elo_home, dtype=float))
    away = np.atleast_1d(np.asarray(elo_away, dtype=float))
    home, away = np.broadcast_arrays(home, away)
    design = _design(home, away, np.ones(home.shape, dtype=bool))
    base = coef[0] + coef[2] * design["level"].to_numpy()
    edge = coef[1] * design["gap"].to_numpy()
    lam_home, lam_away = np.exp(base + edge), np.exp(base - edge)
    if np.ndim(elo_home) == 0 and np.ndim(elo_away) == 0:
        return float(lam_home[0]), float(lam_away[0])
    return lam_home, lam_away


# ---- team-name resolution -----------------------------------------------------------


def known_club_names(raw: dict) -> set:
    """Every team name any covered league has ever used - what an ESPN continental name is
    resolved against. A club belongs to exactly one of these in practice, so a unique hit
    ties its continental results to its domestic ratings."""
    names = set()
    for df in raw.values():
        if df is None or not len(df) or "HomeTeam" not in df.columns:
            continue
        names.update(df["HomeTeam"].astype(str))
        names.update(df["AwayTeam"].astype(str))
    return names


def resolve_names(espn_names: set, known: set) -> tuple:
    """({espn name: global key}, names that couldn't be tied to a covered-league club).
    A name that matches nothing keeps its own spelling as the key: clubs from leagues this
    site doesn't model (Saudi Arabia, Qatar, ...) are still rated off their continental
    results, which is better than skipping their matches entirely. Only an AMBIGUOUS hit is
    left unresolved - guessing the wrong club would weld two different teams onto one rating."""
    resolved, unresolved = {}, []
    for name in sorted(espn_names):
        hit = match_club(name, known)
        resolved[name] = hit or name  # unrated-off-nothing clubs keep ESPN's spelling
        if hit is None:
            unresolved.append(name)
    return resolved, unresolved


# ---- prediction ---------------------------------------------------------------------


def predict_fixtures(fixtures: pd.DataFrame, ratings: dict, form: dict, coef: np.ndarray) -> pd.DataFrame:
    """`fixtures`: ESPN continental rows for matches not yet played, with `home`/`away`
    already mapped to global keys by the caller."""
    if not len(fixtures):
        return pd.DataFrame()
    elo_h = np.array([ratings.get(t, INITIAL_RATING) for t in fixtures["home"]])
    elo_a = np.array([ratings.get(t, INITIAL_RATING) for t in fixtures["away"]])
    lam_h, lam_a = expected_goals(coef, elo_h, elo_a)

    rows = []
    for (_, fx), lh, la, rh, ra in zip(fixtures.iterrows(), lam_h, lam_a, elo_h, elo_a):
        m = market_probs(float(lh), float(la))
        row = {
            "sport": "football",
            "league": fx["competition"],
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
        row["predicted_outcome"] = max(
            ("H", "D", "A"), key=lambda k: row[{"H": "prob_home", "D": "prob_draw", "A": "prob_away"}[k]]
        )
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


def build(raw: dict, soccer: pd.DataFrame, today: dt.date = None, horizon_days: int = 10,
          history: pd.DataFrame = None, now: pd.Timestamp = None) -> dict:
    """Everything the continental side contributes to one prediction run:
      predictions - upcoming continental fixtures, ready for storage.save_predictions
      matches     - continental history for the head-to-head / team-profile pages
      ratings     - current global rating per club that plays each competition
    `raw` is run.load_raw_matches's dict; `soccer` is espn.fetch_soccer's frame (the club
    and national-team sides share the same download - this must not fetch it again)."""
    now = now or pd.Timestamp.now(tz="UTC")
    today = today or now.date()
    if history is None:
        history = continental_history(soccer)
    upcoming = soccer[
        (soccer["kind"] == "continental")
        & (soccer["state"] == "pre")
        & (soccer["kickoff_utc"] >= now)
        & (soccer["kickoff_utc"] < now + pd.Timedelta(days=horizon_days))
    ].copy()

    # Resolve ESPN's spellings to the domestic ones BEFORE the rating pass - a club that
    # plays both its league and the Champions League must be one rating, not two, or its
    # continental results would calibrate a club its domestic matches never touched.
    known = known_club_names(raw)
    names = set(history["home"]) | set(history["away"]) | set(upcoming["home"]) | set(upcoming["away"])
    resolved, unresolved = resolve_names(names, known)
    if unresolved:
        # not a failure: a club from a league this site doesn't model is still rated off its
        # own cross-border results, it just has no domestic anchor to borrow from
        print(f"  ({len(unresolved)} continental club(s) with no covered-league anchor, rated off continental play)")
    if len(history):
        history = history.assign(home=history["home"].map(resolved), away=history["away"].map(resolved))

    pool = pd.concat([domestic_matches(raw), history], ignore_index=True)
    if not len(pool):
        return {"predictions": pd.DataFrame(), "matches": pd.DataFrame(), "ratings": pd.DataFrame()}
    rated, ratings, form = rate_matches(pool)
    coef = fit_goal_model(rated)
    print(
        f"Continental: {len(history)} cross-border matches on a global pool of {len(pool)}; "
        f"{len(ratings)} clubs rated; goal model gap={coef[1]:.3f} level={coef[2]:.3f}"
    )

    upcoming["home"], upcoming["away"] = upcoming["home"].map(resolved), upcoming["away"].map(resolved)
    # the daily feed's `competition` is blank (it's the internationals' label column) - the
    # continental code lives in league_code, and that's what the predictions are keyed on
    upcoming = upcoming.assign(competition=upcoming["league_code"])
    if not len(upcoming):
        return {
            "predictions": pd.DataFrame(),
            "matches": _history_export(history, today),
            "ratings": _ratings_export(history, ratings, today),
        }

    predictions = predict_fixtures(upcoming, ratings, form, coef)
    return {
        "predictions": predictions,
        "matches": _history_export(history, today),
        "ratings": _ratings_export(history, ratings, today),
    }


def _history_export(history: pd.DataFrame, today: dt.date) -> pd.DataFrame:
    """Continental history in storage's matches shape (team names in global-key spelling
    where resolvable), for the h2h/profile pages. Only the last PAGE_HISTORY_SEASONS
    seasons - the pages never show more, and the ratings already carry everything older."""
    if not len(history):
        return pd.DataFrame(columns=["league", "date", "home_team", "away_team", "home_goals", "away_goals", "result"])
    cutoff = pd.Timestamp(today) - pd.DateOffset(years=PAGE_HISTORY_SEASONS)
    recent = history[history["date"] >= cutoff]
    return pd.DataFrame(
        {
            "league": recent["competition"].to_numpy(),
            "date": recent["date"].dt.strftime("%Y-%m-%d").to_numpy(),
            "home_team": recent["home"].to_numpy(),
            "away_team": recent["away"].to_numpy(),
            "home_goals": recent["home_goals"].to_numpy(),
            "away_goals": recent["away_goals"].to_numpy(),
            "result": np.select(
                [recent["home_goals"] > recent["away_goals"], recent["home_goals"] < recent["away_goals"]],
                ["H", "A"], "D",
            ),
        }
    )


def _ratings_export(history: pd.DataFrame, ratings: dict, today: dt.date) -> pd.DataFrame:
    """Each competition's global rating table - every club that played it recently, ranked
    by the one worldwide scale (that's what makes Al Hilal and Real Madrid comparable)."""
    if not len(history):
        return pd.DataFrame(columns=["league", "team", "elo_rating"])
    cutoff = pd.Timestamp(today) - pd.DateOffset(years=PAGE_HISTORY_SEASONS)
    recent = history[history["date"] >= cutoff]
    rows = []
    for code, group in recent.groupby("competition"):
        for team in sorted(set(group["home"]) | set(group["away"])):
            rows.append({"league": code, "team": team, "elo_rating": ratings.get(team, INITIAL_RATING)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")
    pd.set_option("display.width", 250)

    import espn as _espn  # noqa: F811 - local import keeps the module importable without the feed
    import run

    this_year = dt.datetime.now(dt.timezone.utc).year
    print("=== Loading domestic history ===")
    raw = run.load_raw_matches(range(HISTORY_START_YEAR, this_year + 1), force_years={this_year})
    soccer = _espn.fetch_soccer(dt.date(this_year, 1, 1), dt.date.today())
    history = continental_history(soccer)
    rated, ratings, _form = rate_matches(pd.concat([domestic_matches(raw), history], ignore_index=True))

    # walk-forward check: fit the goal model before each year, score that year's continental
    # matches against the rating gap - the honest test of whether the global scale predicts
    # cross-border results at all
    labels = ["A", "D", "H"]
    from sklearn.metrics import accuracy_score, log_loss

    outcome = np.select(
        [rated["home_goals"] > rated["away_goals"], rated["home_goals"] < rated["away_goals"]], ["H", "A"], "D"
    )
    rated = rated.assign(outcome=outcome)
    cont = rated[rated["continental"]]
    print(f"\n{len(cont)} continental matches since {HISTORY_START_YEAR}. Walk-forward:")
    for year in range(max(HISTORY_START_YEAR + 1, this_year - 5), this_year + 1):
        start = pd.Timestamp(f"{year}-01-01")
        test = cont[(cont["date"] >= start) & (cont["date"] < start + pd.DateOffset(years=1))]
        if test.empty:
            continue
        coef = fit_goal_model(rated, as_of=start)
        lam_h, lam_a = expected_goals(coef, test["elo_home"], test["elo_away"])
        proba = np.array([[market_probs(h, a)[k] for k in labels] for h, a in zip(lam_h, lam_a)])
        print(
            f"  {year}: {len(test):>4} matches  accuracy={accuracy_score(test['outcome'], np.array(labels)[proba.argmax(axis=1)]):.3f}"
            f"  log-loss={log_loss(test['outcome'], proba, labels=labels):.3f}"
        )

    top = sorted(ratings.items(), key=lambda kv: kv[1], reverse=True)[:15]
    print("\nTop 15 clubs on the global scale:")
    for team, rating in top:
        print(f"  {team:<28} {rating:.0f}")
