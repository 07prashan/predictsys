"""Everything the website shows, written out as static JSON files.

The site has no server-side logic and no database at runtime: this module turns the SQLite
file predict.py maintains into plain files under server/public/data/, and the page just
fetches them. That's what lets it live on any free static host (Vercel, Netlify, GitHub
Pages, ...) - a host like that has no persistent disk and can't run Python, so the
database and the models stay wherever predict.py runs (a laptop, or a scheduled CI job),
and only these snapshots are deployed.

Files written (all UTF-8 JSON):
  predictions.json       {generated_at, rows}  upcoming matches, with the page's finished-match cutoff
  history.json           recently graded predictions
  scoreboard.json        accuracy / log-loss per league and overall
  teams/<league>.json    {team: profile}       rating, rank, recent form, next fixture
  h2h/<league>.json      {"home|away": {...}}  head-to-head for every pair the pages can show
  slips.json             filter slips: today / 2 / 3 / 4-day accumulators built from the
                         day's most confident low-odds selections (see slips.py)

Team and head-to-head files are split per league so opening one profile downloads one small
file, not every team of every sport.

Usage (regenerate the files from the existing database, without re-running any model):
    python src/site_export.py
"""

import datetime as dt
import json
import math
import os
import re
import sqlite3
from pathlib import Path

import slips
import storage
from fetch_data import LEAGUES

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA_DIR = ROOT / "server" / "public" / "data"
# TheSportsDB crests collected before ESPN supplied them with every match - the fallback for
# club-league rows predicted back then
LEGACY_LOGOS = ROOT / "server" / "logo_cache.json"

# Not leagues but "competition groups": national-team football and the two tennis tours,
# each holding many differently-named competitions/tournaments (stored per prediction).
LEAGUE_NAMES = {
    **LEAGUES,
    "INT": "National Teams (International)",
    "ATP": "ATP Tour (Tennis)",
    "WTA": "WTA Tour (Tennis)",
}
TENNIS_TOURS = {"ATP", "WTA"}

# A match with a kickoff time is over a few hours after it starts; an older row that only has a
# date is over once that date has passed. Deliberately generous (a long tennis match runs for
# hours) - the page applies a tighter per-sport cutoff on top, in the viewer's own clock.
FINISHED_AFTER_HOURS = 6
HISTORY_ROWS = 200
H2H_MEETINGS = 10  # meetings listed per pair (the summary always counts every one)

_UPCOMING_COLUMNS = """league, sport, competition, round, match_date, kickoff_utc, home_team, away_team,
    prob_home, prob_draw, prob_away, predicted_outcome, correct_score_home, correct_score_away,
    btts_yes_prob, over_2_5_prob, markets_json, best_pick_market, best_pick_label, best_pick_prob,
    home_logo, away_logo"""
_HISTORY_COLUMNS = _UPCOMING_COLUMNS + ", actual_outcome, correct, actual_home_goals, actual_away_goals"


# ---- names and grouping (mirrors what the "By Competition" view files things under) ----


def country_of(league: str) -> str:
    match = re.search(r"\(([^)]+)\)$", LEAGUE_NAMES.get(league, ""))
    return match.group(1) if match else league


def competition_of(league: str) -> str:
    name = LEAGUE_NAMES.get(league)
    return re.sub(r"\s*\([^)]+\)$", "", name) if name else league


def group_of(league: str) -> str:
    return f"{league} Tour" if league in TENNIS_TOURS else country_of(league)


# ---- helpers ----


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tidy(value):
    """Floats to 4 decimals: the page shows whole percentages, and 0.5026948197222783 is
    just bytes - it makes the snapshot about 40% bigger for no visible difference."""
    return round(value, 4) if isinstance(value, float) else value


def _write_json(path: Path, payload) -> None:
    """Atomic: a reader (a deploy, a browser) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _query(conn: sqlite3.Connection, sql: str, params=()) -> list:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.row_factory = None


def _query_table(conn: sqlite3.Connection, table: str, sql: str, params=()) -> list:
    """_query for a table predict.py creates on its first export. A database that has only just
    been born (the very first scheduled run, or one where a part failed early) has predictions
    but no match history or ratings yet - that must give empty files, not a crash."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return _query(conn, sql, params) if exists else []


class Logos:
    """name -> crest/flag URL. predict.py stores the image URL the data feed supplied on each
    prediction, and those cover every name the site shows now; the older TheSportsDB cache
    only matters for club rows predicted before that existed."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_name = {}
        for row in _query(
            conn,
            """SELECT home_team AS name, home_logo AS logo FROM predictions WHERE home_logo IS NOT NULL
               UNION ALL SELECT away_team, away_logo FROM predictions WHERE away_logo IS NOT NULL""",
        ):
            self.by_name.setdefault(row["name"], row["logo"])
        try:
            legacy = json.loads(LEGACY_LOGOS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            legacy = {}
        for name, url in legacy.items():
            if url:
                self.by_name.setdefault(name, url)

    def get(self, name: str):
        return self.by_name.get(name)


def _match_row(row: dict, logos: Logos) -> dict:
    """One prediction in the shape the page uses: its supplementary markets (stored as JSON)
    flattened in, plus the display names and grouping the API used to add."""
    extras = {}
    if row.get("markets_json"):
        try:
            extras = json.loads(row["markets_json"])
        except ValueError:
            extras = {}
    out = {k: _tidy(v) for k, v in row.items() if k != "markets_json" and v is not None}
    out.update({k: _tidy(v) for k, v in extras.items()})
    out["sport"] = row.get("sport") or "football"
    out["competition"] = row.get("competition") or competition_of(row["league"])
    out["league_name"] = row.get("competition") or LEAGUE_NAMES.get(row["league"], row["league"])
    out["country"] = group_of(row["league"])
    out["home_logo"] = logos.get(row["home_team"])
    out["away_logo"] = logos.get(row["away_team"])
    return out


# ---- the files ----


def upcoming(conn: sqlite3.Connection, logos: Logos, now: dt.datetime) -> list:
    cutoff = now - dt.timedelta(hours=FINISHED_AFTER_HOURS)
    rows = _query(
        conn,
        f"""SELECT {_UPCOMING_COLUMNS} FROM predictions
            WHERE actual_outcome IS NULL
              AND ((kickoff_utc IS NOT NULL AND kickoff_utc >= ?) OR (kickoff_utc IS NULL AND match_date >= ?))
            ORDER BY COALESCE(kickoff_utc, match_date) ASC""",
        (_iso(cutoff), _iso(cutoff)[:10]),
    )
    return [_match_row(r, logos) for r in rows]


def history(conn: sqlite3.Connection, logos: Logos) -> list:
    rows = _query(
        conn,
        f"""SELECT {_HISTORY_COLUMNS} FROM predictions WHERE actual_outcome IS NOT NULL
            ORDER BY COALESCE(kickoff_utc, match_date) DESC LIMIT ?""",
        (HISTORY_ROWS,),
    )
    return [_match_row(r, logos) for r in rows]


def scoreboard(conn: sqlite3.Connection) -> list:
    rows = _query(
        conn,
        "SELECT league, prob_home, prob_draw, prob_away, actual_outcome, correct FROM predictions WHERE actual_outcome IS NOT NULL",
    )
    if not rows:
        return []
    prob_of = {"H": "prob_home", "D": "prob_draw", "A": "prob_away"}

    def summarize(group):
        n = len(group)
        log_loss = -sum(math.log(max(r[prob_of[r["actual_outcome"]]], 1e-10)) for r in group) / n
        return {"n_settled": n, "accuracy": sum(r["correct"] for r in group) / n, "log_loss": log_loss}

    by_league = {}
    for row in rows:
        by_league.setdefault(row["league"], []).append(row)
    result = [
        {"league": league, "league_name": LEAGUE_NAMES.get(league, league), **summarize(group)}
        for league, group in by_league.items()
    ]
    result.append({"league": "ALL", "league_name": "All leagues", **summarize(rows)})
    return result


def _outcome(result: str, is_home: bool) -> str:
    if result == "D":
        return "D"
    return "W" if (is_home and result == "H") or (not is_home and result == "A") else "L"


def team_profiles(conn: sqlite3.Connection, league: str, logos: Logos, next_fixtures: dict) -> dict:
    """{team: profile} for every rated team in a league - the same figures the API's team
    endpoint produced: rating and rank, last-ten form, and the next fixture."""
    ratings = _query_table(conn, "team_ratings", "SELECT team, elo_rating FROM team_ratings WHERE league = ? ORDER BY elo_rating DESC", (league,))
    if not ratings:
        return {}
    rank = {r["team"]: i + 1 for i, r in enumerate(ratings)}

    recent = {}
    for m in _query_table(conn, "matches", "SELECT * FROM matches WHERE league = ? ORDER BY date DESC", (league,)):
        for team, is_home in ((m["home_team"], True), (m["away_team"], False)):
            if team not in rank or len(recent.setdefault(team, [])) >= 10:
                continue
            opponent = m["away_team"] if is_home else m["home_team"]
            recent[team].append(
                {
                    "date": m["date"],
                    "opponent": opponent,
                    "opponent_logo": logos.get(opponent),
                    "venue": "H" if is_home else "A",
                    "goals_for": _tidy(m["home_goals"] if is_home else m["away_goals"]),
                    "goals_against": _tidy(m["away_goals"] if is_home else m["home_goals"]),
                    "outcome": _outcome(m["result"], is_home),
                }
            )

    profiles = {}
    for r in ratings:
        team, matches = r["team"], recent.get(r["team"], [])
        points = [{"W": 3, "D": 1, "L": 0}[m["outcome"]] for m in matches]
        profiles[team] = {
            "team": team,
            "sport": "tennis" if league in TENNIS_TOURS else "football",
            "league": league,
            "league_name": LEAGUE_NAMES.get(league, league),
            "logo": logos.get(team),
            "elo_rating": round(r["elo_rating"], 1),
            "league_rank": rank[team],
            "league_size": len(ratings),
            "recent_form_ppg": round(sum(points) / len(points), 4) if points else None,
            "recent_win_rate": round(sum(1 for m in matches if m["outcome"] == "W") / len(matches), 4) if matches else None,
            "recent_matches": matches,
            "next_fixture": next_fixtures.get((league, team)),
        }
    return profiles


def h2h(conn: sqlite3.Connection, league: str, home: str, away: str) -> dict:
    meetings = _query_table(
        conn,
        "matches",
        """SELECT date, home_team, away_team, home_goals, away_goals, result FROM matches
           WHERE league = ? AND ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))
           ORDER BY date DESC""",
        (league, home, away, away, home),
    )
    summary = {"home_wins": 0, "away_wins": 0, "draws": 0}
    for m in meetings:
        if m["result"] == "D":
            summary["draws"] += 1
        elif (m["home_team"] == home) == (m["result"] == "H"):
            summary["home_wins"] += 1
        else:
            summary["away_wins"] += 1
    shown = [{k: _tidy(v) for k, v in m.items() if k != "result"} for m in meetings[:H2H_MEETINGS]]
    return {"summary": summary, "total": len(meetings), "meetings": shown}


def filter_slips(upcoming_rows: list, now: dt.datetime, live_odds: bool = False) -> dict:
    """The filter slips snapshot (slips.py). Real 1xLite prices are only fetched when asked
    for - the offline export (and its tests) must never depend on a third-party feed."""
    odds = {}
    # only the fixtures inside the widest slip window can ever appear on a slip, so the
    # (rate-limited) betting feed is asked about those and nothing else
    window_end = slips.day_start(now) + dt.timedelta(days=max(day_count for _id, day_count, _label in slips.FILTER_DAYS))
    in_window = [row for row in upcoming_rows if (slips.kickoff_of(row) or now) < window_end]
    if live_odds and in_window:
        try:
            import xlite

            odds = xlite.odds_for_rows(in_window)
        except Exception as exc:  # noqa: BLE001 - a betting feed being down must not sink the export
            print(f"  (live odds unavailable - slips use model prices: {exc})")
    return slips.build_slips(upcoming_rows, now=now, odds=odds)


def export_site(
    conn: sqlite3.Connection, out_dir: Path = SITE_DATA_DIR, now: dt.datetime = None, live_odds: bool = False
) -> dict:
    """Write every file the site needs. Per-league shards go first and the files the page
    loads on arrival go last, so a reader never sees new predictions with stale shards.

    live_odds pulls current 1xLite prices for the slips - opt-in, so the export stays pure
    and offline by default (predict.py's scheduled run turns it on)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    logos = Logos(conn)
    upcoming_rows = upcoming(conn, logos, now)
    history_rows = history(conn, logos)

    # each team's next fixture, for the profile page
    next_fixtures = {}
    for m in upcoming_rows:
        fixture = {
            k: m.get(k)
            for k in ("league", "match_date", "kickoff_utc", "home_team", "away_team", "prob_home", "prob_draw", "prob_away", "predicted_outcome")
        }
        for team in (m["home_team"], m["away_team"]):
            next_fixtures.setdefault((m["league"], team), fixture)

    leagues = sorted({r["league"] for r in upcoming_rows + history_rows} | {r["league"] for r in _query_table(conn, "team_ratings", "SELECT DISTINCT league FROM team_ratings")})
    n_teams = n_pairs = 0
    for league in leagues:
        profiles = team_profiles(conn, league, logos, next_fixtures)
        if profiles:
            _write_json(out_dir / "teams" / f"{league}.json", profiles)
            n_teams += len(profiles)
        pairs = {(r["home_team"], r["away_team"]) for r in upcoming_rows + history_rows if r["league"] == league}
        if pairs:
            _write_json(out_dir / "h2h" / f"{league}.json", {f"{h}|{a}": h2h(conn, league, h, a) for h, a in sorted(pairs)})
            n_pairs += len(pairs)

    slip_payload = filter_slips(upcoming_rows, now, live_odds)
    slips.write_slips(slip_payload, out_dir / "slips.json")

    _write_json(out_dir / "scoreboard.json", scoreboard(conn))
    _write_json(out_dir / "history.json", history_rows)
    _write_json(out_dir / "predictions.json", {"generated_at": _iso(now), "rows": upcoming_rows})
    n_slips = sum(len(f["slips"]) for f in slip_payload["filters"])
    return {
        "upcoming": len(upcoming_rows), "history": len(history_rows), "teams": n_teams,
        "h2h_pairs": n_pairs, "slips": n_slips,
    }


if __name__ == "__main__":
    connection = storage.connect()
    print("Website data written:", export_site(connection), "->", SITE_DATA_DIR)
    connection.close()
