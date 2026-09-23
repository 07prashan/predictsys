"""Download historical match result CSVs from football-data.co.uk."""

import time
from datetime import date
from pathlib import Path

import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# football-data.co.uk's own league codes, verified against their site (see data.php).
# Switzerland, Norway, Finland, Denmark, and Sweden are NOT here on purpose - they're
# published in a different combined-file format with inconsistent season conventions
# (calendar-year for some, Aug-May for others) and need separate handling; adding them
# to this dict would silently break the season-based logic everywhere else that assumes
# the mmz4281/{season}/{code}.csv shape. Saudi Arabia isn't covered by this source at all.
#
# Second divisions: only England, Scotland, Germany, Italy, Spain, and France actually
# have one on this source (verified by fetching each code directly - the rest 300/301
# the same "multiple choices" stub as any unsupported code, never a second-tier CSV).
# Stopped at one tier below the top flight to match what was actually asked for
# (top flight + second division); League One/Two-level codes exist for England and
# Scotland too but aren't pulled in here.
LEAGUES = {
    "E0": "Premier League (England)",
    "E1": "Championship (England)",
    "SP1": "La Liga (Spain)",
    "SP2": "Segunda Division (Spain)",
    "D1": "Bundesliga (Germany)",
    "D2": "2. Bundesliga (Germany)",
    "I1": "Serie A (Italy)",
    "I2": "Serie B (Italy)",
    "F1": "Ligue 1 (France)",
    "F2": "Ligue 2 (France)",
    "P1": "Primeira Liga (Portugal)",
    "N1": "Eredivisie (Netherlands)",
    "G1": "Super League (Greece)",
    "T1": "Super Lig (Turkey)",
    "B1": "Jupiler Pro League (Belgium)",
    "SC0": "Premiership (Scotland)",
    "SC1": "Championship (Scotland)",
}


def season_code(start_year: int) -> str:
    """2017 -> '1718' (the 2017/18 season)."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season_start_year(today: date = None) -> int:
    """European domestic seasons run roughly August to May, so from July onward
    "this season" starts this calendar year; before that, it started last year."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def fetch_season(start_year: int, league: str = "E0", force: bool = False, retries: int = 3) -> Path | None:
    """Returns the cached file's path, or None if this league hasn't got a season
    started yet for `start_year`. football-data.co.uk answers a file that doesn't
    exist with an HTML "multiple choices" page (not a 404), so the status code
    alone can't tell us that - the response body has to be checked too."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    season = season_code(start_year)
    dest = RAW_DIR / f"{league}_{season}.csv"
    if dest.exists() and not force:
        return dest
    url = BASE_URL.format(season=season, league=league)
    for attempt in range(retries):
        resp = requests.get(url, timeout=30)
        if resp.status_code == 429 and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))  # back off and give the free site a break
            continue
        if resp.status_code == 200 and not resp.content.lstrip()[:15].lower().startswith(b"<!doctype"):
            dest.write_bytes(resp.content)
            return dest
        print(f"  (no {league} data yet for {start_year}/{(start_year + 1) % 100:02d} - skipping)")
        return None
    return None


def fetch_seasons(start_years, league: str = "E0", force: bool = False, force_years: set = None) -> list[Path]:
    """force_years: seasons to re-fetch even if cached, on top of `force` - for an
    in-progress season, which gains new results every week, unlike a completed one."""
    force_years = force_years or set()
    paths = []
    for year in start_years:
        paths.append(fetch_season(year, league, force=force or year in force_years))
        time.sleep(1.0)  # this is a small free dataset run by one person - don't hammer it
    return paths
