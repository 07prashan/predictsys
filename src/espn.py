"""Fixtures AND results for national-team football, the 17 club leagues, and ATP/WTA
tennis, from ESPN's public scoreboard feed.

football-data.co.uk (fetch_fixtures.py) only publishes the NEXT round of club fixtures,
and nothing at all while the leagues are paused for an international window - so a
week-ahead view built on it alone is empty exactly when national teams are playing.
ESPN's feed knows the schedule well in advance, covers national teams and tennis too,
and carries a kickoff time (UTC) for every match.

This is an undocumented endpoint: no key, no SLA, and it can change shape without
warning. So nothing in here raises on a bad response - a failed day/week is logged and
skipped, and the callers treat "ESPN returned nothing" as a normal, survivable outcome
rather than something that should take the whole prediction run down with it.

Football comes from the combined "all soccer" daily feed (one request per day instead
of one per league per day), filtered to the league ids below. Don't pass limit > 1000
to it: ESPN silently answers a bigger limit with a near-empty page (seen: limit=2000
returned 25 events where limit=1000 returned all 424).
"""

import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SITE_API = "https://site.api.espn.com/apis/site/v2/sports"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# ESPN's numeric league ids, read from each league's own scoreboard (they're stable,
# unlike league names, which ESPN rewords between seasons).
INTERNATIONAL_COMPETITIONS = {
    3922: "International Friendly",
    2395: "UEFA Nations League",
    19267: "Concacaf Nations League",
    3947: "UEFA Euro Qualifying",
    786: "World Cup Qualifying (UEFA)",
    787: "World Cup Qualifying (CONMEBOL)",
    788: "World Cup Qualifying (Concacaf)",
    790: "World Cup Qualifying (CAF)",
    789: "World Cup Qualifying (AFC)",
    792: "World Cup Qualifying (OFC)",
    606: "FIFA World Cup",
    781: "UEFA European Championship",
    780: "Copa America",
    3908: "Africa Cup of Nations",
    20219: "AFC Asian Cup",
    4004: "Concacaf Gold Cup",
}

# ESPN league id -> football-data.co.uk league code, so a club fixture from here lands in
# the same per-league models/tables the rest of the pipeline already keys on.
CLUB_LEAGUES = {
    700: "E0", 3914: "E1",
    740: "SP1", 3921: "SP2",
    720: "D1", 3927: "D2",
    730: "I1", 3931: "I2",
    710: "F1", 3926: "F2",
    715: "P1", 725: "N1", 3955: "G1", 3946: "T1", 3901: "B1",
    735: "SC0", 3940: "SC1",
}

SOCCER_COLUMNS = [
    "external_id", "kickoff_utc", "league_id", "kind", "league_code", "competition", "state", "status",
    "home", "away", "home_logo", "away_logo", "home_form", "away_form", "home_goals", "away_goals",
]
TENNIS_COLUMNS = [
    "external_id", "tour", "tournament", "major", "round", "kickoff_utc", "state", "status",
    "p1", "p2", "p1_flag", "p2_flag", "winner", "p1_sets", "p2_sets", "score_text",
]

_session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = "predictsys/1.0 (personal project; contact via repo)"
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
        _session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
    return _session


def get_json(url: str, params: dict = None) -> dict | None:
    """None (after logging) on any failure - see the module docstring for why this never raises."""
    try:
        resp = _get_session().get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  (ESPN request failed, skipping: {url} {params or ''} - {exc})")
        return None


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(pd.to_datetime(value, utc=True))


# ---- Football ----------------------------------------------------------------------


def _parse_soccer_event(event: dict) -> dict | None:
    match = re.search(r"~l:(\d+)", event.get("uid", ""))
    league_id = int(match.group(1)) if match else None
    if league_id in INTERNATIONAL_COMPETITIONS:
        kind, league_code, competition = "intl", "INT", INTERNATIONAL_COMPETITIONS[league_id]
    elif league_id in CLUB_LEAGUES:
        kind, league_code, competition = "club", CLUB_LEAGUES[league_id], None
    else:
        return None

    comp = (event.get("competitions") or [{}])[0]
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    if "home" not in sides or "away" not in sides:
        return None
    status = comp.get("status", {}).get("type", {})
    state = status.get("state", "pre")

    def score(side):
        raw = side.get("score")
        return int(raw) if state == "post" and raw not in (None, "") else None

    return {
        "external_id": f"espn:{event['id']}",
        "kickoff_utc": _utc(event["date"]),
        "league_id": league_id,
        "kind": kind,
        "league_code": league_code,
        "competition": competition,
        "state": state,
        "status": status.get("name", ""),
        "home": sides["home"]["team"]["displayName"],
        "away": sides["away"]["team"]["displayName"],
        "home_logo": sides["home"]["team"].get("logo"),
        "away_logo": sides["away"]["team"].get("logo"),
        "home_form": sides["home"].get("form"),
        "away_form": sides["away"].get("form"),
        "home_goals": score(sides["home"]),
        "away_goals": score(sides["away"]),
    }


def _soccer_day_records(day: dt.date) -> list | None:
    """Parsed records for one day, or None if the request itself failed (an empty list
    means ESPN answered and there genuinely were no tracked matches)."""
    data = get_json(f"{SITE_API}/soccer/all/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 1000})
    if data is None:
        return None
    records = []
    for event in data.get("events", []):
        try:
            record = _parse_soccer_event(event)
        except (KeyError, IndexError, ValueError, TypeError):
            continue  # one malformed event shouldn't cost the rest of the day
        if record:
            records.append(record)
    return records


def _cached_soccer_day(day: dt.date, final_before: dt.date) -> list | None:
    """Days safely in the past are final - they're cached on disk so a daily run only
    ever re-fetches the recent/upcoming days that can still change."""
    path = CACHE_DIR / "espn_soccer" / f"{day:%Y%m%d}.json"
    if day < final_before and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    records = _soccer_day_records(day)
    if records is not None and day < final_before:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, default=str), encoding="utf-8")
    return records


def fetch_soccer(start: dt.date, end: dt.date, workers: int = 6) -> pd.DataFrame:
    """Every tracked national-team and club-league match from start to end inclusive.
    ESPN's day boundaries aren't UTC's, so a late kickoff can sit under the neighbouring
    day - callers should filter on kickoff_utc, not on the day they asked for."""
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    final_before = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda d: _cached_soccer_day(d, final_before), days))

    records = [r for day_records in results if day_records for r in day_records]
    failed = sum(1 for r in results if r is None)
    if failed:
        print(f"  (ESPN football: {failed} of {len(days)} day request(s) failed)")
    if not records:
        return pd.DataFrame(columns=SOCCER_COLUMNS)
    df = pd.DataFrame(records)
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    df = df.drop_duplicates("external_id").sort_values("kickoff_utc").reset_index(drop=True)
    for col in ("home_goals", "away_goals"):
        df[col] = df[col].astype("Int64")
    return df[SOCCER_COLUMNS]


# ---- Tennis ------------------------------------------------------------------------

# Rounds that are never part of the main draw: not predicted, and not rated either -
# the history dataset only holds tour-level main-draw matches, so rating qualifiers
# off a different population would skew the ratings of anyone who plays both.
_NON_MAIN_DRAW = re.compile(r"qualif", re.IGNORECASE)
_PLACEHOLDER_NAME = re.compile(r"\b(tbd|bye|qualifier|winner of|loser of)\b", re.IGNORECASE)


def _sets_won(competitor: dict) -> int:
    return sum(1 for line in competitor.get("linescores", []) if line.get("winner"))


def _parse_tennis_match(comp: dict, event: dict, tour: str) -> dict | None:
    if _NON_MAIN_DRAW.search(comp.get("round", {}).get("displayName", "")):
        return None
    sides = sorted(comp.get("competitors", []), key=lambda c: c.get("order", 0))
    if len(sides) != 2:
        return None
    athletes = [s.get("athlete") for s in sides]
    if not all(athletes) or any(_PLACEHOLDER_NAME.search(a.get("displayName", "")) for a in athletes):
        return None  # a draw slot still waiting on an earlier round's winner

    status = comp.get("status", {}).get("type", {})
    state = status.get("state", "pre")
    winner = 0
    if state == "post":
        winner = next((i + 1 for i, s in enumerate(sides) if s.get("winner")), 0)
    notes = comp.get("notes") or [{}]
    return {
        "external_id": f"espn:{comp['uid']}",
        "tour": tour,
        "tournament": event["name"],
        "major": bool(event.get("major")),
        "round": comp.get("round", {}).get("displayName", ""),
        "kickoff_utc": _utc(comp["startDate"]),
        "state": state,
        "status": status.get("name", ""),
        "p1": athletes[0]["displayName"],
        "p2": athletes[1]["displayName"],
        "p1_flag": (athletes[0].get("flag") or {}).get("href"),
        "p2_flag": (athletes[1].get("flag") or {}).get("href"),
        "winner": winner,
        "p1_sets": _sets_won(sides[0]),
        "p2_sets": _sets_won(sides[1]),
        "score_text": notes[0].get("text", ""),
    }


def _tennis_records(tour: str, day: dt.date | None) -> list | None:
    params = {"dates": day.strftime("%Y%m%d")} if day else None
    data = get_json(f"{SITE_API}/tennis/{tour.lower()}/scoreboard", params)
    if data is None:
        return None
    singles = "mens-singles" if tour == "ATP" else "womens-singles"
    records = []
    for event in data.get("events", []):
        # a joint ATP+WTA event (e.g. the China Open) shows up in BOTH tours' feeds with
        # both draws inside it - only this tour's own singles draw belongs to it
        if event.get("status", {}).get("type", {}).get("description") == "Canceled":
            continue
        for grouping in event.get("groupings", []):
            if grouping.get("grouping", {}).get("slug") != singles:
                continue
            for comp in grouping.get("competitions", []):
                try:
                    record = _parse_tennis_match(comp, event, tour)
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                if record:
                    records.append(record)
    return records


def _cached_tennis_week(tour: str, day: dt.date, final_before: dt.date) -> list | None:
    """A request for any day inside a tournament returns that tournament's WHOLE draw,
    so stepping through history a week at a time covers it. A week that's wholly in
    the past can't change any more and is cached on disk."""
    path = CACHE_DIR / "espn_tennis" / tour.lower() / f"{day:%Y%m%d}.json"
    if day < final_before and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    records = _tennis_records(tour, day)
    if records is not None and day < final_before:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, default=str), encoding="utf-8")
    return records


def fetch_tennis(tour: str, days: list, workers: int = 4) -> pd.DataFrame:
    """Main-draw singles matches for `tour` ('ATP' or 'WTA') from every tournament that
    was running on any of `days`. Duplicates (a two-week event seen from two different
    days) are collapsed by match id, keeping the latest state of each."""
    final_before = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=3)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda d: _cached_tennis_week(tour, d, final_before), days))

    records = [r for week in results if week for r in week]
    failed = sum(1 for r in results if r is None)
    if failed:
        print(f"  (ESPN {tour}: {failed} of {len(days)} request(s) failed)")
    if not records:
        return pd.DataFrame(columns=TENNIS_COLUMNS)
    df = pd.DataFrame(records)
    df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"], utc=True)
    # a finished copy of a match beats a scheduled copy of the same match
    df["_final"] = (df["state"] == "post").astype(int)
    df = df.sort_values(["external_id", "_final"]).drop_duplicates("external_id", keep="last")
    return df.drop(columns="_final").sort_values("kickoff_utc").reset_index(drop=True)[TENNIS_COLUMNS]
