"""Filter slips: accumulators built from the day's most confident low-odds selections.

For every upcoming match the model already prices a whole board of markets. This module
picks, per match, the SINGLE selection the model is most confident in whose decimal odds sit
in a low-risk band (default 1.10-1.40) - the "best prediction" for that price - and then
combines selections from different matches into slips whose total odds fall in 2.0-4.5.

Odds come from the 1xLite betting app when xlite.py can resolve the fixture there; otherwise
the model's fair odds (1 / probability) stand in, and each leg records which was used.

A "day" runs 04:00-04:00 in Kathmandu time (UTC+5:45) rather than midnight, matching how a
betting day is usually read - Oct 9 means 4am Oct 9 to 4am Oct 10 (Kathmandu), not the
calendar date.
Four filters are produced from the same run time: today, and the next 2, 3 and 4 days.

The result is a plain JSON snapshot (like everything else the site shows), so the page needs
no server and this can be regenerated without re-running any model:

    python src/slips.py
"""

import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "server" / "public" / "data" / "slips.json"

# A "day" is 04:00 -> 04:00 in Kathmandu time, so a late kickoff belongs to the night it was
# played on rather than the calendar date the clock rolled past midnight. The user reads the
# filters in Kathmandu, so the boundary must be 4am THERE (22:15 UTC the previous day),
# not 4am UTC (= 9:45am Kathmandu). Kathmandu has no DST, one fixed offset forever.
DAY_START_HOUR = 4
DAY_TZ = dt.timezone(dt.timedelta(hours=5, minutes=45))  # Asia/Kathmandu
DAY_TZ_NAME = "Asia/Kathmandu"

# Each leg must be a low-risk, low-price favourite: the whole point of a slip is many
# near-certain legs rather than one long shot.
LEG_ODDS_MIN = 1.10
LEG_ODDS_MAX = 1.40

# And the finished slip has to be worth placing, but not a lottery ticket.
SLIP_ODDS_MIN = 2.00
SLIP_ODDS_MAX = 4.50
MAX_LEGS = 12  # a 12-leg 1.4 acca is already 56x; a slip this long is as far as we go

# The filters the page offers: how many of the 04:00-day cycles the slip may draw from.
FILTER_DAYS = [("today", 1, "Today"), ("days_2", 2, "2 Days"), ("days_3", 3, "3 Days"), ("days_4", 4, "4 Days")]

# (selection code, market, label) - codes match xlite.parse_odds() exactly, so a real price
# can be dropped in wherever the model would have used its own.
_FOOTBALL_SELECTIONS = [
    ("1", "Result", "Home Win"),
    ("X", "Result", "Draw"),
    ("2", "Result", "Away Win"),
    ("1X", "Double Chance", "Home or Draw"),
    ("12", "Double Chance", "Home or Away"),
    ("X2", "Double Chance", "Draw or Away"),
    ("O1.5", "Over/Under", "Over 1.5 Goals"),
    ("U1.5", "Over/Under", "Under 1.5 Goals"),
    ("O2.5", "Over/Under", "Over 2.5 Goals"),
    ("U2.5", "Over/Under", "Under 2.5 Goals"),
    ("O3.5", "Over/Under", "Over 3.5 Goals"),
    ("U3.5", "Over/Under", "Under 3.5 Goals"),
    ("BTTS", "Both Teams to Score", "Both Teams to Score"),
    ("BTTS_NO", "Both Teams to Score", "Not Both Teams to Score"),
]


def _num(value):
    """A finite float, or None - guards against missing markets (a legacy row, a tennis
    row) and against NaN sneaking in from a partly-populated source."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def probability(row, code: str):
    """The model's probability for one selection code, or None when the row can't price it."""
    if (row.get("sport") or "football") == "tennis":
        return _num(row.get("prob_home")) if code == "1" else _num(row.get("prob_away")) if code == "2" else None

    if code in ("1", "X", "2"):
        return _num(row.get({"1": "prob_home", "X": "prob_draw", "2": "prob_away"}[code]))
    if code in ("1X", "12", "X2"):
        # the stored double-chance markets are the source of truth; the plain sum is the
        # fallback for a row logged before those columns existed
        fallback = {"1X": ("prob_home", "prob_draw"), "12": ("prob_home", "prob_away"), "X2": ("prob_draw", "prob_away")}[code]
        stored = _num(row.get(f"double_chance_{code.lower()}_prob"))
        if stored is not None:
            return stored
        parts = [_num(row.get(col)) for col in fallback]
        return sum(parts) if all(p is not None for p in parts) else None
    if code.startswith("O") or code.startswith("U"):
        line = code[1:]
        over = _num(row.get(f"over_{line.replace('.', '_')}_prob"))
        if over is None:
            return None
        return over if code.startswith("O") else 1 - over
    if code == "BTTS":
        return _num(row.get("btts_yes_prob"))
    if code == "BTTS_NO":
        yes = _num(row.get("btts_yes_prob"))
        return None if yes is None else 1 - yes
    return None


def selection_label(row, code: str) -> str:
    """What the leg says on the slip - the away football labels come from the fixed table,
    while a tennis "to win" leg names the player it backs."""
    if (row.get("sport") or "football") == "tennis":
        return f"{row.get('home_team') if code == '1' else row.get('away_team')} to win"
    return dict((c, label) for c, _market, label in _FOOTBALL_SELECTIONS).get(code, code)


def selection_market(row, code: str) -> str:
    if (row.get("sport") or "football") == "tennis":
        return "Match Winner"
    return dict((c, market) for c, market, _label in _FOOTBALL_SELECTIONS).get(code, "Market")


def match_key(row) -> tuple:
    return (row.get("league"), row.get("home_team"), row.get("away_team"))


def kickoff_of(row):
    """The match's kickoff as a tz-aware UTC datetime, or None if the row carries no date."""
    raw = row.get("kickoff_utc")
    if raw:
        try:
            moment = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    date = row.get("match_date")
    if date:
        try:
            return dt.datetime.fromisoformat(str(date)).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def day_start(now: dt.datetime) -> dt.datetime:
    """The 04:00 Kathmandu-time boundary that began the betting day `now` falls in, in UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local = now.astimezone(DAY_TZ)
    start = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local < start:
        start -= dt.timedelta(days=1)
    return start.astimezone(dt.timezone.utc)


def best_leg(row, prices: dict = None):
    """The model's most likely selection whose odds fall in the band - the single best
    prediction for this match at a low price. Real prices win when we have them, so a
    selection the bookmaker doesn't actually offer at that price is never used."""
    real = bool(prices)
    best = None
    for code, market, label in _FOOTBALL_SELECTIONS if (row.get("sport") or "football") != "tennis" else [("1", "Match Winner", ""), ("2", "Match Winner", "")]:
        prob = probability(row, code)
        if prob is None or prob <= 0:
            continue
        if real:
            odds = _num(prices.get(code))
            if odds is None:
                continue
        else:
            odds = 1.0 / prob  # the model's fair decimal price
        if not (LEG_ODDS_MIN <= odds <= LEG_ODDS_MAX):
            continue
        candidate = (prob, code, market, label, odds)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    prob, code, market, label, odds = best
    return {
        "match_key": "|".join(str(part) for part in match_key(row)),
        "league": row.get("league"),
        "competition": row.get("competition") or row.get("league"),
        "sport": row.get("sport") or "football",
        "kickoff_utc": (kickoff_of(row) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "home_logo": row.get("home_logo"),
        "away_logo": row.get("away_logo"),
        "selection": code,
        "market": market,
        "label": label or selection_label(row, code),
        "prob": round(prob, 4),
        "odds": round(odds, 3),
        "odds_source": "1xlite" if real else "model",
    }


def _order(legs, strategy: str):
    if strategy == "safest":
        return sorted(legs, key=lambda leg: (-leg["prob"], leg["odds"], leg["kickoff_utc"]))
    if strategy == "soonest":
        return sorted(legs, key=lambda leg: (leg["kickoff_utc"], -leg["prob"]))
    return sorted(legs, key=lambda leg: (-leg["odds"], -leg["prob"]))  # biggest-price legs first


# Each target is built with a different ordering, so the three slips on a filter are genuinely
# different combinations rather than one slip with extra legs bolted on.
_SLIP_PLAN = [(2.0, "safest"), (3.0, "soonest"), (4.5, "biggest")]


def build_slip(legs: list, target: float, strategy: str):
    """Greedily stack distinct matches (highest-priority first) until the running price
    reaches `target`, never crossing the upper bound."""
    total, chosen, used = 1.0, [], set()
    for leg in _order(legs, strategy):
        if len(chosen) >= MAX_LEGS or total >= target:
            break
        if leg["match_key"] in used or total * leg["odds"] > SLIP_ODDS_MAX:
            continue
        total *= leg["odds"]
        used.add(leg["match_key"])
        chosen.append(leg)
    if not chosen or total < SLIP_ODDS_MIN:
        return None
    return {
        "target": target,
        "total_odds": round(total, 2),
        "win_prob": round(_product_prob(chosen), 4),
        "legs": chosen,
    }


def _product_prob(legs) -> float:
    prob = 1.0
    for leg in legs:
        prob *= leg["prob"]
    return prob


def build_filter(rows, prices: dict, now: dt.datetime, day_count: int) -> dict:
    """One filter's window, its eligible legs, and up to three slips inside 2.0-4.5."""
    start = day_start(now)
    end = start + dt.timedelta(days=day_count)
    legs = []
    for row in rows:
        when = kickoff_of(row)
        if when is None or not (start <= when < end):
            continue
        leg = best_leg(row, prices.get(match_key(row)))
        if leg is not None:
            legs.append(leg)

    slips, seen = [], set()
    for target, strategy in _SLIP_PLAN:
        slip = build_slip(legs, target, strategy)
        if slip is None:
            continue
        signature = tuple(leg["match_key"] for leg in slip["legs"])
        if signature in seen:
            continue
        seen.add(signature)
        slips.append(slip)
    slips.sort(key=lambda slip: slip["total_odds"])

    return {
        "id": None,
        "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_available": len(legs),
        "slips": slips,
    }


def build_slips(rows, now: dt.datetime = None, odds: dict = None) -> dict:
    """The whole snapshot: `rows` is whatever the site would show as upcoming, `odds` is
    {(league, home_team, away_team): {code: price}} from xlite (optional)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prices = odds or {}
    filters = []
    for filter_id, day_count, label in FILTER_DAYS:
        entry = build_filter(rows, prices, now, day_count)
        entry["id"] = filter_id
        entry["label"] = label
        entry["day_count"] = day_count
        filters.append(entry)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day_start_hour": DAY_START_HOUR,
        "day_start_tz": DAY_TZ_NAME,
        "leg_odds_range": [LEG_ODDS_MIN, LEG_ODDS_MAX],
        "slip_odds_range": [SLIP_ODDS_MIN, SLIP_ODDS_MAX],
        "n_matches_with_real_odds": len(prices),
        "filters": filters,
    }


def write_slips(payload: dict, out_path: Path = OUT_PATH) -> Path:
    """Atomically, so a deploy or a browser never reads a half-written file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, out_path)
    return out_path


def main() -> None:
    """Regenerate slips.json straight from the prediction database, without the site export."""
    import site_export
    import storage

    conn = storage.connect()
    try:
        rows = site_export.upcoming(conn, site_export.Logos(conn), dt.datetime.now(dt.timezone.utc))
    finally:
        conn.close()
    odds = {}
    try:
        import xlite

        odds = xlite.odds_for_rows(rows)
    except Exception as exc:  # noqa: BLE001 - the standalone tool works offline too
        print(f"  (live odds unavailable - using model prices: {exc})")
    payload = build_slips(rows, odds=odds)
    path = write_slips(payload)
    print(f"{sum(len(f['slips']) for f in payload['filters'])} slips written -> {path}")


if __name__ == "__main__":
    main()
