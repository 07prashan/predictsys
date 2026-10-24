"""Download historical match result CSVs from football-data.co.uk."""

import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# Top flights outside Europe, from football-data.co.uk's "new format" files: one CSV per
# country holding that country's whole history (all seasons in one file, a different column
# layout from the per-season European files, and - for Argentina - a second cup competition
# in the same file that must be filtered out). Calendar-year seasons, so `season` is just the
# year the file labels them with, not the mmz4281 two-year convention.
# code -> (display league, country, the League column's value that marks the top flight)
WORLD_LEAGUES = {
    "ARG": ("Liga Profesional", "Argentina", "Liga Profesional"),
    "BRA": ("Serie A", "Brazil", "Serie A"),
    "MEX": ("Liga MX", "Mexico", "Liga MX"),
    "USA": ("Major League Soccer", "USA", "MLS"),
    "JPN": ("J1 League", "Japan", "J1 League"),
    "CHN": ("Super League", "China", "Super League"),
    # European top flights published in the same one-file-per-country format. Denmark and
    # Russia are followed closely enough to be full-coverage leagues; Norway and Sweden are
    # published only when the model is confident (see site_export.STRONG_PICKS_ONLY).
    #
    # Romania, Finland and Ireland are NOT here: football-data publishes their results, but
    # no fixture feed does (ESPN's rou.1/irl.1 scoreboards are frozen in 2025 and fin.1 is
    # empty), so a model for them could never be shown a match to predict. Adding them is a
    # one-line change once a fixture source covers them.
    "DNK": ("Superliga", "Denmark", "Superliga"),
    "RUS": ("Premier League", "Russia", "Premier League"),
    "NOR": ("Eliteserien", "Norway", "Eliteserien"),
    "SWE": ("Allsvenskan", "Sweden", "Allsvenskan"),
}
WORLD_LEAGUE_NAMES = {code: f"{name} ({country})" for code, (name, country, _league_in_csv) in WORLD_LEAGUES.items()}
WORLD_FIRST_SEASON = 2017  # matches the European leagues' history window, so every model starts together

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

# every covered league - the European top flights and second divisions plus the world leagues
ALL_LEAGUES = {**LEAGUES, **WORLD_LEAGUE_NAMES}


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


# ---- world leagues (the "new format" files) ----------------------------------------

WORLD_BASE_URL = "https://football-data.co.uk/new/{code}.csv"
WORLD_MAX_AGE_HOURS = 12.0  # an in-progress calendar-year season gains results weekly


def fetch_world_league(code: str, force: bool = False, max_age_hours: float = WORLD_MAX_AGE_HOURS) -> Path | None:
    """The whole country file (every season in one CSV), cached on disk and re-downloaded
    only when the cache is stale - unlike the European per-season files, one download here
    covers every season at once, so a daily run refetches this one file and nothing more.
    Returns None (with the failure reported) rather than raising: a world league that can't
    be refreshed keeps its cached history instead of taking the whole prediction run down."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / f"{code}.csv"
    if dest.exists() and not force:
        age_hours = (time.time() - dest.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return dest
    for attempt in range(3):
        try:
            resp = requests.get(WORLD_BASE_URL.format(code=code), timeout=30)
        except requests.RequestException as exc:
            if dest.exists():
                print(f"  (couldn't refresh {code} - using the cached copy: {exc})")
                return dest
            print(f"  (no {code} data available: {exc})")
            return None
        if resp.status_code == 429 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 200 and not resp.content.lstrip()[:15].lower().startswith(b"<!doctype"):
            dest.write_bytes(resp.content)
            return dest
        print(f"  (no {code} data available - skipping)")
        return None
    return None


def load_world_league(code: str, force: bool = False, min_season: int = WORLD_FIRST_SEASON) -> pd.DataFrame:
    """One world league's results, normalized to exactly the frame the rest of the pipeline
    expects from a league: Date, HomeTeam, AwayTeam, FTHG, FTAG, FTR, season (start year).

    The new-format files carry a different column layout (Home/Away/HG/AG/Res instead of
    HomeTeam/.../FTHG) and label seasons either '2026' or '2026/2027' depending on the
    country's calendar - both are read from the file rather than assumed, and only the top
    flight named in WORLD_LEAGUES is kept (Argentina's file also carries the Copa de la
    Liga, a separate competition whose results must not leak into the league model)."""
    path = fetch_world_league(code, force=force)
    if path is None:
        return pd.DataFrame(columns=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "season"])
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="cp1252")

    league_in_csv = WORLD_LEAGUES[code][2]
    if "League" in df.columns:
        df = df[df["League"].astype(str).str.strip() == league_in_csv]
    df = df.dropna(subset=["Home", "Away", "Res"]).copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df.dropna(subset=["Date"])
    # '2026' or '2026/2027' - either way the season starts in the first year named
    df["season"] = df["Season"].astype(str).str.strip().str.slice(0, 4).astype(int)
    df = df[df["season"] >= min_season]
    return pd.DataFrame(
        {
            "Date": df["Date"],
            "HomeTeam": df["Home"].astype(str).str.strip(),
            "AwayTeam": df["Away"].astype(str).str.strip(),
            "FTHG": pd.to_numeric(df["HG"], errors="coerce"),
            "FTAG": pd.to_numeric(df["AG"], errors="coerce"),
            "FTR": df["Res"].astype(str).str.strip().str.upper().str[0],
            "season": df["season"],
        }
    ).dropna(subset=["FTHG", "FTAG"]).sort_values("Date").reset_index(drop=True)
