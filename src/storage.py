"""SQLite storage for predictions, so a track record accumulates across runs.

Predictions are only ever INSERTed, never overwritten: the prediction that
counts for grading is the one made before the match, with whatever
information was available then - not whatever a later rerun would guess
with a week's more form data. Settling a fixture (filling in what actually
happened) is the only way an existing row ever changes.

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
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    for name, col_type in NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {name} {col_type}")
    conn.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    _migrate(conn)
    return conn


def save_predictions(conn: sqlite3.Connection, predictions: pd.DataFrame, predicted_at: str) -> int:
    """predictions needs: league, match_date (YYYY-MM-DD string), home_team, away_team,
    prob_home, prob_draw, prob_away, predicted_outcome, expected_goals_home/away,
    correct_score_home/away, btts_yes_prob, over_2_5_prob, best_pick_market/label/prob.
    Returns how many were newly inserted (a fixture already logged from an earlier run
    is left untouched)."""
    inserted = 0
    with conn:
        for _, row in predictions.iterrows():
            extra_markets = {
                k: (float(row[k]) if pd.notna(row[k]) else None)
                for k in (
                    "over_1_5_prob", "over_3_5_prob",
                    "double_chance_1x_prob", "double_chance_x2_prob", "double_chance_12_prob",
                    "home_clean_sheet_prob", "away_clean_sheet_prob",
                )
                if k in row
            }
            cur = conn.execute(
                """INSERT OR IGNORE INTO predictions
                   (predicted_at, league, match_date, home_team, away_team,
                    prob_home, prob_draw, prob_away, predicted_outcome,
                    expected_goals_home, expected_goals_away,
                    correct_score_home, correct_score_away,
                    btts_yes_prob, over_2_5_prob,
                    best_pick_market, best_pick_label, best_pick_prob, markets_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    float(row["expected_goals_home"]),
                    float(row["expected_goals_away"]),
                    int(row["correct_score_home"]),
                    int(row["correct_score_away"]),
                    float(row["btts_yes_prob"]),
                    float(row["over_2_5_prob"]),
                    row["best_pick_market"],
                    row["best_pick_label"],
                    float(row["best_pick_prob"]),
                    json.dumps(extra_markets),
                ),
            )
            inserted += cur.rowcount
    return inserted


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


def save_matches(conn: sqlite3.Connection, matches: pd.DataFrame) -> None:
    """matches needs: league, date (YYYY-MM-DD string), home_team, away_team, home_goals,
    away_goals, result. Full overwrite each run - this mirrors the source data (needed for
    head-to-head history and recent form on team/match detail pages), it isn't something
    to preserve old versions of the way logged predictions are."""
    matches.to_sql("matches", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_teams ON matches(home_team, away_team)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league)")
    conn.commit()


def save_team_ratings(conn: sqlite3.Connection, ratings: pd.DataFrame) -> None:
    """ratings needs: league, team, elo_rating. A current snapshot, overwritten each run -
    for team profile pages (current rating + rank within league)."""
    ratings.to_sql("team_ratings", conn, if_exists="replace", index=False)
    conn.commit()
