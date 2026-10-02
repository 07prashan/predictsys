"""Upcoming fixtures (not yet played) from football-data.co.uk's combined feed.

Unlike the per-season, per-league result files in fetch_data.py, this is one
file covering every league they track, refreshed as the next round of
fixtures is confirmed - so it's never cached, only ever fetched fresh.

It only ever lists the NEXT round, and is empty while the leagues are paused for
an international window. predict.py therefore takes club fixtures from ESPN
(espn.py) first and uses this feed as the fallback for anything ESPN doesn't have.
"""

import io

import pandas as pd
import requests

from fetch_data import LEAGUES

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"


def fetch_fixtures() -> pd.DataFrame:
    """Upcoming fixtures for our covered leagues: league, Date, HomeTeam, AwayTeam,
    kickoff_utc (NaT when the feed gives no time - it's UK local time, converted here)."""
    resp = requests.get(FIXTURES_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8-sig")

    df = df[df["Div"].isin(LEAGUES)].copy()
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam"])
    df = df.rename(columns={"Div": "league"})

    if "Time" in df.columns:
        local = pd.to_datetime(df["Date"].dt.strftime("%Y-%m-%d") + " " + df["Time"].astype(str), errors="coerce")
        df["kickoff_utc"] = local.dt.tz_localize("Europe/London", ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    else:
        df["kickoff_utc"] = pd.NaT
    return df[["league", "Date", "HomeTeam", "AwayTeam", "kickoff_utc"]].sort_values("Date").reset_index(drop=True)
