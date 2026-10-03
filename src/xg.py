"""Match-level expected-goals (xG) data from Understat, joined onto
football-data.co.uk match rows by (season, HomeTeam, AwayTeam).

Understat and football-data.co.uk spell team names differently ("Man City"
vs "Manchester City", "Ath Madrid" vs "Atletico Madrid", ...). TEAM_NAME_MAP
below was built by pulling the actual team name sets used by both sources
across 2017-2026 and comparing them directly - not guessed.
"""

import time
from pathlib import Path

import pandas as pd
from understatapi import UnderstatClient

XG_DIR = Path(__file__).resolve().parent.parent / "data" / "xg"

UNDERSTAT_LEAGUE = {
    "E0": "EPL",
    "SP1": "La_Liga",
    "D1": "Bundesliga",
    "I1": "Serie_A",
    "F1": "Ligue_1",
}

# understat name -> football-data.co.uk name. Teams not listed here are spelled the same in both.
TEAM_NAME_MAP = {
    "E0": {
        "Manchester City": "Man City",
        "Manchester United": "Man United",
        "Newcastle United": "Newcastle",
        "Nottingham Forest": "Nott'm Forest",
        "West Bromwich Albion": "West Brom",
        "Wolverhampton Wanderers": "Wolves",
    },
    "SP1": {
        "Athletic Club": "Ath Bilbao",
        "Atletico Madrid": "Ath Madrid",
        "Real Betis": "Betis",
        "Celta Vigo": "Celta",
        "Espanyol": "Espanol",
        "SD Huesca": "Huesca",
        "Deportivo La Coruna": "La Coruna",
        "Real Oviedo": "Oviedo",
        "Real Sociedad": "Sociedad",
        "Real Valladolid": "Valladolid",
        "Rayo Vallecano": "Vallecano",
    },
    "D1": {
        "Arminia Bielefeld": "Bielefeld",
        "Bayer Leverkusen": "Leverkusen",
        "Borussia Dortmund": "Dortmund",
        "Borussia M.Gladbach": "M'gladbach",
        "Eintracht Frankfurt": "Ein Frankfurt",
        "FC Cologne": "FC Koln",
        "FC Heidenheim": "Heidenheim",
        "Fortuna Duesseldorf": "Fortuna Dusseldorf",
        "Greuther Fuerth": "Greuther Furth",
        "Hamburger SV": "Hamburg",
        "Hannover 96": "Hannover",
        "Hertha Berlin": "Hertha",
        "Mainz 05": "Mainz",
        "Nuernberg": "Nurnberg",
        "RasenBallsport Leipzig": "RB Leipzig",
        "St. Pauli": "St Pauli",
        "VfB Stuttgart": "Stuttgart",
    },
    "I1": {
        "AC Milan": "Milan",
        "Parma Calcio 1913": "Parma",
        "SPAL 2013": "Spal",
    },
    "F1": {
        "Clermont Foot": "Clermont",
        "Paris Saint Germain": "Paris SG",
        "Saint-Etienne": "St Etienne",
    },
}


def _cache_path(league_code: str, year: int) -> Path:
    return XG_DIR / f"{league_code}_{year}.csv"


def fetch_xg(league_code: str, start_years, force_years: set = None) -> pd.DataFrame:
    """One row per finished match: season, team names (translated to
    football-data.co.uk's spelling), and each side's actual xG.

    force_years: seasons to re-fetch even if cached - for an in-progress season,
    which gains new finished matches every week, unlike a completed one."""
    if league_code not in UNDERSTAT_LEAGUE:
        # Understat only covers the "big five" - every other league merges in as
        # all-NaN xG, which features.py already treats as "no xG for this match"
        # rather than a fabricated number, same as any other missing entry.
        return pd.DataFrame(columns=["season", "HomeTeam", "AwayTeam", "home_xg", "away_xg"])

    XG_DIR.mkdir(parents=True, exist_ok=True)
    understat_league = UNDERSTAT_LEAGUE[league_code]
    name_map = TEAM_NAME_MAP.get(league_code, {})
    force_years = force_years or set()

    frames = []
    with UnderstatClient() as client:
        for year in start_years:
            path = _cache_path(league_code, year)
            if not path.exists() or year in force_years:
                try:
                    rows = []
                    for match in client.league(league=understat_league).get_match_data(season=str(year)):
                        if not match.get("isResult"):
                            continue
                        home = name_map.get(match["h"]["title"], match["h"]["title"])
                        away = name_map.get(match["a"]["title"], match["a"]["title"])
                        rows.append(
                            {
                                "season": year,
                                "HomeTeam": home,
                                "AwayTeam": away,
                                "home_xg": float(match["xG"]["h"]),
                                "away_xg": float(match["xG"]["a"]),
                            }
                        )
                    pd.DataFrame(rows).to_csv(path, index=False)
                    time.sleep(1.0)  # this is a scraped free resource - don't hammer it
                except Exception as exc:  # noqa: BLE001 - a scraped site can fail in many ways
                    # A refresh of a season we already have is a nice-to-have: yesterday's xG is
                    # nearly as good, and losing the whole club-league run over it (say, from a
                    # cloud IP the site blocks) would be far worse. With no copy at all, there is
                    # nothing to fall back to, so the failure stands.
                    if not path.exists():
                        raise
                    print(f"  (couldn't refresh {league_code} {year} xG - using the cached copy: {exc})")
            frames.append(pd.read_csv(path))
    return pd.concat(frames, ignore_index=True)
