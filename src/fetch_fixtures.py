"""Upcoming fixtures (not yet played) from football-data.co.uk's combined feed.

Unlike the per-season, per-league result files in fetch_data.py, this is one
file covering every league they track, refreshed as the next round of
fixtures is confirmed - so it's never cached, only ever fetched fresh.
"""

import io

import pandas as pd
import requests

from fetch_data import LEAGUES

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"


def fetch_fixtures() -> pd.DataFrame:
    """Upcoming fixtures for our covered leagues: league, Date, HomeTeam, AwayTeam."""
    resp = requests.get(FIXTURES_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8-sig")

    df = df[df["Div"].isin(LEAGUES)].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam"])
    df = df.rename(columns={"Div": "league"})
    return df[["league", "Date", "HomeTeam", "AwayTeam"]].sort_values("Date").reset_index(drop=True)
