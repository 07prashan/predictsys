"""SQLite storage for predictions, so a track record accumulates across runs.

Predictions are only ever INSERTed, never overwritten: the prediction that
counts for grading is the one made before the match, with whatever
information was available then - not whatever a later rerun would guess
with a week's more form data. Settling a fixture (filling in what actually
happened) is the only way an existing row's prediction or result ever changes;
the one other edit is rescheduling (a moved date or newly-learned kickoff time,
which says nothing about the prediction itself).

Only the raw actual score is stored on settle (actual_home_goals/away_goals),
not a separate correct/incorrect flag per market - whether a correct-score,
BTTS, or over/under pick was right is derived from that raw score wherever
it's needed, rather than duplicated as stored booleans that could drift out
of sync with the score they're supposed to summarize.
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    predicted_at TEXT NOT NULL,
    league TEXT NOT NULL,
    match_date TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    prob_home REAL NOT NULL,
    prob_draw REAL NOT NULL,
    prob_away REAL NOT NULL,
    predicted_outcome TEXT NOT NULL,
    actual_outcome TEXT,
    correct INTEGER,
    UNIQUE(league, match_date, home_team, away_team)
)
"""

# Added after the original schema - migrated in with ALTER TABLE rather than
# assumed present, so an existing database (with real logged predictions)
# upgrades in place instead of needing to be recreated.
NEW_COLUMNS = {
    "expected_goals_home": "REAL",
    "expected_goals_away": "REAL",
    "correct_score_home": "INTEGER",
    "correct_score_away": "INTEGER",
    "btts_yes_prob": "REAL",
    "over_2_5_prob": "REAL",
    "best_pick_market": "TEXT",
    "best_pick_label": "TEXT",
    "best_pick_prob": "REAL",
    "actual_home_goals": "INTEGER",
    "actual_away_goals": "INTEGER",
    # every market beyond the ones above (over/under at other lines, double chance,
    # clean sheets, ...) as a JSON blob - these are supplementary detail-page markets,
    # not used by the scoreboard/settle logic, so one flexible column beats a growing
    # pile of single-purpose ones
    "markets_json": "TEXT",
    # multi-sport support. A row with no `sport` is a club-league football match from
    # before these existed (the API treats NULL as 'football'). For tennis, home/away are
    # player 1 / player 2, prob_draw is 0, and the "goal" columns hold sets.
    "sport": "TEXT",
    "competition": "TEXT",  # tournament / competition name, where the league code alone isn't specific enough
    "round": "TEXT",
    "kickoff_utc": "TEXT",  # ISO-8601 UTC, so the website can show local time and know when a match is over
    "external_id": "TEXT",  # ESPN's id for the match - what lets a rescheduled match be updated, not duplicated
    "home_logo": "TEXT",
    "away_logo": "TEXT",
}

# Per-match extras that live in markets_json: supplementary markets, plus the context
# (recent form, ratings, tennis surface) shown on the match card and detail page.
EXTRA_COLUMNS = (
    "over_1_5_prob", "over_3_5_prob",
    "double_chance_1x_prob", "double_chance_x2_prob", "double_chance_12_prob",
    "home_clean_sheet_prob", "away_clean_sheet_prob",
    "home_form", "away_form", "home_rating", "away_rating",
    "surface", "best_of", "grand_slam", "straight_sets_prob", "goes_the_distance_prob", "sets_line", "sets_over_prob",
    "limited_data",
    # basketball: the predicted scoreline's context - an over/under line and the model's
    # total/spread, none of which football or tennis carries
    "total_line", "over_prob", "under_prob", "predicted_total", "predicted_spread",
)


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    for name, col_type in NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {name} {col_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_external_id ON predictions(external_id)")
    conn.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    _migrate(conn)
    return conn


def _missing(value) -> bool:
    return value is None or bool(pd.isna(value))


def _num(value, cast=float):
    """NULL for a missing value - a tennis row has no goal markets, and SQLite should
    store that as NULL rather than choke on a NaN."""
    return None if _missing(value) else cast(value)


def _text(value) -> str | None:
    return None if _missing(value) else str(value)


def _iso_utc(value) -> str | None:
    if _missing(value):
        return None
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value):
    return value.item() if hasattr(value, "item") else value  # numpy scalars aren't JSON-serializable


def _reschedule(conn: sqlite3.Connection, row_id: int, row: pd.Series, kickoff: str | None) -> None:
    """A match we've already predicted has moved (or the feed has since learned its kickoff
    time or crests): update where/when it is - never the prediction itself, which stays
    the one made before the match."""
    try:
        conn.execute(
            """UPDATE predictions SET match_date = ?, kickoff_utc = COALESCE(?, kickoff_utc),
               round = COALESCE(?, round), competition = COALESCE(?, competition),
               home_logo = COALESCE(home_logo, ?), away_logo = COALESCE(away_logo, ?)
               WHERE id = ?""",
            (
                row["match_date"], kickoff, _text(row.get("round")), _text(row.get("competition")),
                _text(row.get("home_logo")), _text(row.get("away_logo")), row_id,
            ),
        )
    except sqlite3.IntegrityError:
        pass  # the new date collides with another stored row for the same fixture - keep the old one


def save_predictions(conn: sqlite3.Connection, predictions: pd.DataFrame, predicted_at: str) -> int:
    """predictions needs: league, match_date (YYYY-MM-DD string), home_team, away_team,
    prob_home, prob_draw, prob_away, predicted_outcome, best_pick_market/label/prob.
    Football adds expected_goals_home/away, correct_score_home/away, btts_yes_prob and
    over_2_5_prob (tennis leaves those out - correct_score_* then means predicted sets).
    Optional: sport, competition, round, kickoff_utc, external_id, home_logo, away_logo,
    plus any EXTRA_COLUMNS (stored in markets_json).

    Returns how many were newly inserted. A match already logged from an earlier run keeps
    its original prediction - only its schedule (date, kickoff time, crests) can change."""
    inserted = 0
    with conn:
        for _, row in predictions.iterrows():
            extras = {k: _plain(row[k]) for k in EXTRA_COLUMNS if k in row and not _missing(row[k])}
            kickoff = _iso_utc(row.get("kickoff_utc"))
            external_id = _text(row.get("external_id"))

            if external_id:
                existing = conn.execute(
                    "SELECT id, actual_outcome FROM predictions WHERE external_id = ?", (external_id,)
                ).fetchone()
                if existing:
                    if existing[1] is None:
                        _reschedule(conn, existing[0], row, kickoff)
                    continue

            cur = conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (predicted_at, league, match_date, home_team, away_team,
                    prob_home, prob_draw, prob_away, predicted_outcome,
                    expected_goals_home, expected_goals_away,
                    correct_score_home, correct_score_away,
                    btts_yes_prob, over_2_5_prob,
                    best_pick_market, best_pick_label, best_pick_prob, markets_json,
                    sport, competition, round, kickoff_utc, external_id, home_logo, away_logo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    predicted_at,
                    row["league"],
                    row["match_date"],
                    row["home_team"],
                    row["away_team"],
                    float(row["prob_home"]),
                    float(row["prob_draw"]),
                    float(row["prob_away"]),
                    row["predicted_outcome"],
                    _num(row.get("expected_goals_home")),
                    _num(row.get("expected_goals_away")),
                    _num(row.get("correct_score_home"), int),
                    _num(row.get("correct_score_away"), int),
                    _num(row.get("btts_yes_prob")),
                    _num(row.get("over_2_5_prob")),
                    row["best_pick_market"],
                    row["best_pick_label"],
                    float(row["best_pick_prob"]),
                    json.dumps(extras),
                    _text(row.get("sport")) or "football",
                    _text(row.get("competition")),
                    _text(row.get("round")),
                    kickoff,
                    external_id,
                    _text(row.get("home_logo")),
                    _text(row.get("away_logo")),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                # the same fixture was already stored (e.g. predicted earlier off football-data's
                # feed, which has no kickoff time or crests) - fill in what only this source knows
                conn.execute(
                    """UPDATE predictions SET kickoff_utc = COALESCE(kickoff_utc, ?), external_id = COALESCE(external_id, ?),
                       home_logo = COALESCE(home_logo, ?), away_logo = COALESCE(away_logo, ?)
                       WHERE league = ? AND match_date = ? AND home_team = ? AND away_team = ? AND actual_outcome IS NULL""",
                    (
                        kickoff, external_id, _text(row.get("home_logo")), _text(row.get("away_logo")),
                        row["league"], row["match_date"], row["home_team"], row["away_team"],
                    ),
                )
    return inserted


def settle_by_external_id(conn: sqlite3.Connection, results: pd.DataFrame) -> int:
    """results: external_id, outcome ('H'/'D'/'A'), home_score, away_score (goals for
    football, sets for tennis). Settles the stored predictions with a matching ESPN id -
    no team-name matching involved, so it works for any sport and any naming quirks.
    Returns how many rows were newly settled."""
    if results.empty:
        return 0
    pending = {
        ext: (id_, predicted)
        for id_, ext, predicted in conn.execute(
            "SELECT id, external_id, predicted_outcome FROM predictions WHERE actual_outcome IS NULL AND external_id IS NOT NULL"
        )
    }
    settled = 0
    with conn:
        for r in results.itertuples(index=False):
            if r.external_id not in pending:
                continue
            id_, predicted = pending[r.external_id]
            conn.execute(
                """UPDATE predictions SET actual_outcome = ?, correct = ?,
                   actual_home_goals = ?, actual_away_goals = ? WHERE id = ?""",
                (r.outcome, int(predicted == r.outcome), int(r.home_score), int(r.away_score), id_),
            )
            settled += 1
    return settled


def settle(conn: sqlite3.Connection, results_by_league: dict, window_days: int = 21) -> int:
    """results_by_league: {league_code: DataFrame with Date, HomeTeam, AwayTeam, FTR, FTHG, FTAG}.
    Fills in actual_outcome/correct/actual_home_goals/actual_away_goals for any stored
    prediction whose fixture now has a real result within `window_days` of the predicted
    date (a small window rather than an exact-date match, since a postponed fixture can
    move by a few days). Returns how many rows were newly settled."""
    pending = conn.execute(
        "SELECT id, league, match_date, home_team, away_team, predicted_outcome FROM predictions "
        "WHERE actual_outcome IS NULL"
    ).fetchall()

    settled = 0
    with conn:
        for id_, league, match_date, home, away, predicted_outcome in pending:
            results = results_by_league.get(league)
            if results is None:
                continue
            target_date = pd.Timestamp(match_date)
            candidates = results[(results["HomeTeam"] == home) & (results["AwayTeam"] == away)]
            candidates = candidates[(candidates["Date"] - target_date).abs() <= pd.Timedelta(days=window_days)]
            if candidates.empty:
                continue
            result = candidates.iloc[0]
            conn.execute(
                """UPDATE predictions SET actual_outcome = ?, correct = ?,
                   actual_home_goals = ?, actual_away_goals = ? WHERE id = ?""",
                (
                    result["FTR"],
                    int(predicted_outcome == result["FTR"]),
                    int(result["FTHG"]),
                    int(result["FTAG"]),
                    id_,
                ),
            )
            settled += 1
    return settled


def scoreboard(conn: sqlite3.Connection) -> pd.DataFrame:
    """Accuracy and log-loss-ish summary per league, over every settled prediction ever made."""
    df = pd.read_sql(
        "SELECT * FROM predictions WHERE actual_outcome IS NOT NULL", conn, parse_dates=["match_date"]
    )
    if df.empty:
        return df
    prob_col = {"H": "prob_home", "D": "prob_draw", "A": "prob_away"}
    df["prob_of_actual"] = df.apply(lambda r: r[prob_col[r["actual_outcome"]]], axis=1)

    rows = []
    for league, g in df.groupby("league"):
        rows.append(
            {
                "league": league,
                "n_settled": len(g),
                "accuracy": g["correct"].mean(),
                "log_loss": -np.log(g["prob_of_actual"].clip(lower=1e-10)).mean(),
            }
        )
    rows.append(
        {
            "league": "ALL",
            "n_settled": len(df),
            "accuracy": df["correct"].mean(),
            "log_loss": -np.log(df["prob_of_actual"].clip(lower=1e-10)).mean(),
        }
    )
    return pd.DataFrame(rows)


def _replace_leagues(conn: sqlite3.Connection, table: str, frame: pd.DataFrame) -> None:
    """Swap in `frame` for just the leagues it contains, leaving every other league's rows
    alone - so a run that only refreshes tennis (or national teams) doesn't wipe the club
    leagues' history, and each part of predict.py can export on its own."""
    leagues = frame["league"].unique().tolist()
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    if exists:
        conn.execute(f"DELETE FROM {table} WHERE league IN ({','.join('?' * len(leagues))})", leagues)
        frame.to_sql(table, conn, if_exists="append", index=False)
    else:
        frame.to_sql(table, conn, if_exists="replace", index=False)
    conn.commit()


def save_matches(conn: sqlite3.Connection, matches: pd.DataFrame) -> None:
    """matches needs: league, date (YYYY-MM-DD string), home_team, away_team, home_goals,
    away_goals, result. Overwrites those leagues' rows each run - this mirrors the source data
    (needed for head-to-head history and recent form on team/match detail pages), it isn't
    something to preserve old versions of the way logged predictions are. For tennis the
    winner is stored on the home side (result always 'H') and the goal columns hold sets."""
    _replace_leagues(conn, "matches", matches)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(home_team, away_team)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league)")
    conn.commit()


def save_team_ratings(conn: sqlite3.Connection, ratings: pd.DataFrame) -> None:
    """ratings needs: league, team, elo_rating. A current snapshot per league, overwritten each
    run - for team profile pages (current rating + rank within league)."""
    _replace_leagues(conn, "team_ratings", ratings)
