"""Live match odds from the 1xLite betting app (1xBet's LineFeed API).

The website's own predictions answer "what will happen"; this answers "at what price".
It reads the same football line the betting app shows for one match - 1X2, double chance,
over/under totals and both-teams-to-score - so `slips.py` can pick the selection whose
price sits in the requested band instead of falling back to the model's fair odds.

Three endpoints do the work (all under /service-api/LineFeed/):
  GetChampsZip?sport=1   every competition, with its numeric id (read once, see LEAGUE_IDS)
  GetChampZip?champ=<id> the upcoming games in one competition (teams + kickoff + game id)
  GetGameZip?id=<id>     one game's full market list

Odds are matched to our fixtures by team name, not by id: the two feeds share no id, and a
game matched to the WRONG fixture would price somebody else's match. Matching is therefore
strict - both names must resolve, and the pairing must be unique in that competition - and
anything ambiguous is skipped rather than guessed (the same rule team_match.py follows for
ESPN's names).

Odds are a nice-to-have: every function here returns empty on failure, so an unreachable
feed, a changed id or a rate limit costs nothing but the model-implied prices.
"""

import time

import requests

from team_match import _covers, _tokens

BASE = "https://1xlite-1700550.com/service-api/LineFeed"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://1xlite-1700550.com/en/line/football",
}
TIMEOUT = 15

# Competition ids read from GetChampsZip?sport=1 (name -> id) and checked against the fixture
# it opens. Hard-coded rather than looked up on every run: the ids have been stable, and a
# lookup that silently returns nothing would take the odds down with it. refresh_league_ids()
# prints a fresh list if one ever moves.
LEAGUE_IDS = {
    "E0": 88637,   # England. Premier League
    "E1": 105759,  # England. Championship
    "SP1": 127733,  # Spain. La Liga
    "SP2": 27687,  # Spain. Segunda Division
    "D1": 96463,   # Germany. Bundesliga
    "D2": 109313,  # Germany. 2. Bundesliga
    "I1": 110163,  # Italy. Serie A
    "I2": 7067,    # Italy. Serie B
    "F1": 12821,   # France. Ligue 1
    "F2": 12829,   # France. Ligue 2
    "P1": 3007689,  # Portugal. Primeira Liga
    "N1": 2018750,  # Netherlands. Eredivisie
    "G1": 8777,    # Greece. SuperLeague
    "T1": 11113,   # Turkey. SuperLiga
    "B1": 28787,   # Belgium. Jupiler League
    "SC0": 13521,  # Scotland. Premier League
    "SC1": 281713,  # Scotland. Championship
}

# MarketType ids inside one game's market list, grouped by the market they belong to.
_GROUP_1X2 = 1       # T: 1 home, 2 draw, 3 away
_GROUP_DOUBLE = 8    # T: 4 1X, 5 12, 6 X2
_GROUP_TOTAL = 17    # T: 9 over line P, 10 under line P
_GROUP_BTTS = 19     # T: 180 yes, 181 no

# Totals we price (the model only supplies these three lines).
TOTAL_LINES = {1.5: "1.5", 2.5: "2.5", 3.5: "3.5"}


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _value(path: str, params: dict, session: requests.Session = None):
    """The `Value` field of a LineFeed reply, or None - never raises. A missing id, a rate
    limit or the site being briefly unavailable all just mean 'no odds this time'."""
    session = session or new_session()
    try:
        resp = session.get(f"{BASE}/{path}", params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json().get("Value")
    except (requests.RequestException, ValueError):
        return None


def parse_odds(markets: list) -> dict:
    """One game's market list -> {'1': 1.26, 'X': 6.7, '1X': 1.04, 'O2.5': 1.38,
    'BTTS': 1.79, ...}, decimal odds keyed the same way slips.py names a selection."""
    odds = {}
    for entry in markets or []:
        price = entry.get("C")
        if price is None:
            continue
        group, market_type, line = entry.get("G"), entry.get("T"), entry.get("P")
        if group == _GROUP_1X2:
            code = {1: "1", 2: "X", 3: "2"}.get(market_type)
        elif group == _GROUP_DOUBLE:
            code = {4: "1X", 5: "12", 6: "X2"}.get(market_type)
        elif group == _GROUP_TOTAL:
            side = {9: "O", 10: "U"}.get(market_type)
            code = f"{side}{TOTAL_LINES[line]}" if side and line in TOTAL_LINES else None
        elif group == _GROUP_BTTS:
            code = {180: "BTTS", 181: "BTTS_NO"}.get(market_type)
        else:
            code = None
        if code:
            odds[code] = float(price)
    return odds


def fetch_champ_games(champ_id: int, session: requests.Session = None) -> list:
    """Upcoming games in one competition: {id, home, away, kickoff} (kickoff is a unix
    timestamp). The response nests each game with its period derivatives under 'SG'; only
    the full-time game itself (the one carrying the team names) is of interest."""
    value = _value("GetChampZip", {"champ": champ_id, "lng": "en"}, session)
    if not isinstance(value, dict):
        return []
    games = []
    for game in value.get("G", []):
        home, away, game_id = game.get("O1"), game.get("O2"), game.get("CI")
        if game_id is None or not home or not away or home == "Home" or away == "Away":
            continue
        games.append({"id": game_id, "home": home, "away": away, "kickoff": game.get("S")})
    return games


def fetch_game_odds(game_id: int, session: requests.Session = None) -> dict:
    """One game's prices, keyed by selection code (see parse_odds)."""
    value = _value("GetGameZip", {"id": game_id, "lng": "en"}, session)
    if not isinstance(value, dict):
        return {}
    return parse_odds(value.get("E"))


def fetch_champs(session: requests.Session = None) -> list:
    """Every competition the app lists for football: [{'id', 'name'}, ...]."""
    value = _value("GetChampsZip", {"sport": 1, "lng": "en"}, session)
    if not isinstance(value, list):
        return []
    return [{"id": row.get("LI"), "name": row.get("L")} for row in value if row.get("LI")]


def same_team(a: str, b: str) -> bool:
    """True if two spellings name the same club ('Man City' / 'Manchester City')."""
    if not a or not b:
        return False
    ta, tb = _tokens(a), _tokens(b)
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return _covers(shorter, longer)


def _resolve_pair(games: list, home: str, away: str):
    """The single game in `games` whose two sides both match (home, away), or None when none
    or more than one does - an ambiguous pairing is skipped, never guessed."""
    hits = [g for g in games if same_team(g["home"], home) and same_team(g["away"], away)]
    return hits[0] if len(hits) == 1 else None


def odds_for_rows(rows, max_games: int = 80, max_seconds: float = 60.0, session: requests.Session = None, log=print) -> dict:
    """Real odds for whichever of our prediction `rows` can be resolved on the betting app.

    Returns {(league, home_team, away_team): {code: odds}}; an unresolved or unpriced match
    is simply absent. Bounded by max_games fetches and max_seconds of wall time, since this
    runs inside the site export: a slow feed must not hold the refresh up."""
    football = [r for r in rows if r.get("league") in LEAGUE_IDS and (r.get("sport") or "football") != "tennis"]
    by_league = {}
    for row in football:
        by_league.setdefault(row["league"], []).append(row)

    session = session or new_session()
    started = time.monotonic()
    odds, fetched, skipped = {}, 0, 0
    for league, league_rows in sorted(by_league.items()):
        # only spend requests on leagues that actually have fixtures in the window
        games = fetch_champ_games(LEAGUE_IDS[league], session)
        if not games:
            continue
        for row in league_rows:
            if fetched >= max_games or time.monotonic() - started > max_seconds:
                skipped += 1
                continue
            game = _resolve_pair(games, row.get("home_team"), row.get("away_team"))
            if game is None:
                continue
            prices = fetch_game_odds(game["id"], session)
            fetched += 1
            if prices:
                odds[(league, row["home_team"], row["away_team"])] = prices
    if skipped:
        log(f"  (odds: stopped after {fetched} games / {max_seconds:.0f}s - {skipped} fixture(s) left at model odds)")
    return odds


def refresh_league_ids(country_names=("England", "Spain", "Germany", "Italy", "France", "Portugal", "Netherlands", "Greece", "Turkey", "Belgium", "Scotland")) -> None:
    """Print today's competition ids so LEAGUE_IDS can be checked/updated by hand."""
    for champ in fetch_champs():
        if any(champ["name"].startswith(f"{c}.") for c in country_names):
            print(f"{champ['id']:>8}  {champ['name']}")


if __name__ == "__main__":
    refresh_league_ids()
