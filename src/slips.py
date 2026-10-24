"""Filter slips: accumulators built from the day's most confident low-odds selections.

For every upcoming match the model already prices a whole board of markets. This module
picks, per match, the SINGLE selection the model is most confident in whose decimal odds sit
in a low-risk band (default 1.10-1.40) - the "best prediction" for that price - and then
combines selections from different matches into slips whose total odds fall in 2.0-4.5.
Legs come only from top-tier football - the world's top flights and the continental
competitions, never second divisions (see NON_TOP_TIER_LEAGUES) - and, among legs the model
likes about equally, the side that keeps winning is preferred (see CONSISTENCY_WEIGHT): the
top flight's in-form clubs are what these slips are built around.

Odds come from the 1xLite betting app when xlite.py can resolve the fixture there; otherwise
the model's fair odds (1 / probability) stand in, and each leg records which was used.

A "day" runs 04:00-04:00 in Kathmandu time (UTC+5:45) rather than midnight, matching how a
betting day is usually read - Oct 9 means 4am Oct 9 to 4am Oct 10 (Kathmandu), not the
calendar date.
Six filters are produced from the same run time: 1, 2, 3, 4, 7 and 14 days. Each filter
carries a headline "main slip" (every leg 1.10-1.30, total 2.00-3.50) plus up to three wider
combination slips.

Every finished slip also carries a five-character slip code (letters and digits, no
lookalikes) - the shape a bookmaker's app prints when you save a bet slip - plus the same
slip packed into one longer string (PSB1-<base64url>) that survives a data rebuild. 1xBet
mints its own codes and accepts no other, so the page hands these picks over as text for
1xBet's bet slip; the short code is what this site reopens a slip with.

The result is a plain JSON snapshot (like everything else the site shows), so the page needs
no server and this can be regenerated without re-running any model:

    python src/slips.py
"""

import base64
import datetime as dt
import hashlib
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

# Second divisions: never slip material. The slips are a top-flight product - the legs are
# the world's top-tier leagues (Europe's top flights, Argentina, Brazil, Mexico, MLS, Japan,
# China) plus the continental competitions, where the most consistently performing sides
# live and where a short price actually means what it says. A promotion-race favourite in a
# second division is a different kind of bet, and it isn't ours.
NON_TOP_TIER_LEAGUES = {"E1", "SP2", "D2", "I2", "F2", "SC1"}

# A fixture whose backed side has won its last five is the same bet as one backing a side
# that has lost its last five, as far as the model's single-match probability is concerned -
# form is already one of its features. At the slip level the two are not equivalent: a slip
# is a chain of legs, so it wants the sides that do this every week. Each win in the last
# five adds this much to a leg's ranking score, which tops out at a 7.5% edge - enough to
# win a near-tie, never enough to displace a leg the model rates clearly safer.
CONSISTENCY_WEIGHT = 0.015
FORM_LENGTH = 5

# Each leg must be a low-risk, low-price favourite: the whole point of a slip is many
# near-certain legs rather than one long shot.
LEG_ODDS_MIN = 1.10
LEG_ODDS_MAX = 1.40

# And the finished slip has to be worth placing, but not a lottery ticket.
SLIP_ODDS_MIN = 2.00
SLIP_ODDS_MAX = 4.50
MAX_LEGS = 12  # a 12-leg 1.4 acca is already 56x; a slip this long is as far as we go

# The "main slip" is the one headline bet for a window: every leg is a short price
# (1.10-1.30, so nothing riskier than a near-certainty) and the whole thing still has to
# pay at least double without ever going past 3.5.
MAIN_LEG_ODDS_MIN = 1.10
MAIN_LEG_ODDS_MAX = 1.30
MAIN_SLIP_ODDS_MIN = 2.00
MAIN_SLIP_ODDS_MAX = 3.50

# The filters the page offers: how many of the 04:00-day cycles the slip may draw from.
FILTER_DAYS = [
    ("day_1", 1, "1 Day"),
    ("days_2", 2, "2 Days"),
    ("days_3", 3, "3 Days"),
    ("days_4", 4, "4 Days"),
    ("days_7", 7, "7 Days"),
    ("weeks_2", 14, "2 Weeks"),
]

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


def form_wins(form) -> int | None:
    """How many of the last five the side won (0-5), or None when the feed gave no form.
    ESPN's form string is newest-last ('WWDLW'), so the tail is the most recent run."""
    if not form:
        return None
    recent = [c for c in str(form).upper() if c in "WDL"][-FORM_LENGTH:]
    return sum(1 for c in recent if c == "W") if recent else None


def backed_side(row, code: str):
    """(team, form, rating) for the side a selection is actually backing, or Nones for a
    market that backs nobody (totals, both-teams-to-score, the draw)."""
    side = {"1": "home", "1X": "home", "2": "away", "X2": "away"}.get(code)
    if side is None or (row.get("sport") or "football") != "football":
        return None, None, None
    return row.get(f"{side}_team"), row.get(f"{side}_form"), row.get(f"{side}_rating")


def leg_score(leg: dict) -> float:
    """A leg's probability, nudged towards the sides that have been winning consistently
    (see CONSISTENCY_WEIGHT) - the ranking the "safest" slip is built on."""
    wins = form_wins(leg.get("backed_form"))
    return leg["prob"] * (1 + CONSISTENCY_WEIGHT * (wins if wins is not None else 0))


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


def best_leg(row, prices: dict = None, lo: float = LEG_ODDS_MIN, hi: float = LEG_ODDS_MAX):
    """The model's most likely selection whose odds fall in the band - the single best
    prediction for this match at a low price. Real prices win when we have them, so a
    selection the bookmaker doesn't actually offer at that price is never used."""
    real = bool(prices)
    # The slips are a football/tennis product: their selection codes match xlite's football
    # board. A basketball row has none of those markets, so it is simply not a slip leg.
    sport = row.get("sport") or "football"
    if sport == "tennis":
        selections = [("1", "Match Winner", ""), ("2", "Match Winner", "")]
    elif sport == "football":
        selections = _FOOTBALL_SELECTIONS
    else:
        return None
    best = None
    for code, market, label in selections:
        prob = probability(row, code)
        if prob is None or prob <= 0:
            continue
        if real:
            odds = _num(prices.get(code))
            if odds is None:
                continue
        else:
            odds = 1.0 / prob  # the model's fair decimal price
        if not (lo <= odds <= hi):
            continue
        candidate = (prob, code, market, label, odds)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    prob, code, market, label, odds = best
    backed_team, backed_form, backed_rating = backed_side(row, code)
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
        # whose form/rating this leg is actually backing (None for a totals/BTTS leg) -
        # leg_score uses the form, and the slip shows it for context
        "backed_team": backed_team,
        "backed_form": backed_form,
        "backed_rating": backed_rating,
    }


def _order(legs, strategy: str):
    if strategy == "safest":
        # probability first, then the consistency nudge to order near-ties
        return sorted(legs, key=lambda leg: (-round(leg_score(leg), 4), -leg["prob"], leg["odds"], leg["kickoff_utc"]))
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


def build_main_slip(legs: list):
    """The one headline bet for a window: the surest selections, every leg priced within
    1.10-1.30, stacked until the slip pays at least 2.00 and never more than 3.50. Legs are
    taken most-likely-first - with the consistency nudge ordering near-ties, the same way the
    "safest" combination slip is ordered - so the finished slip is as safe as it can be."""
    if not legs:
        return None
    ordered = sorted(legs, key=lambda leg: (-round(leg_score(leg), 4), -leg["prob"], leg["odds"], leg["kickoff_utc"]))
    total, chosen, used = 1.0, [], set()
    for leg in ordered:
        if total >= MAIN_SLIP_ODDS_MIN or len(chosen) >= MAX_LEGS:
            break
        if leg["match_key"] in used:
            continue
        if total * leg["odds"] > MAIN_SLIP_ODDS_MAX:
            continue  # would blow past the ceiling - a shorter leg later can still fit
        total *= leg["odds"]
        used.add(leg["match_key"])
        chosen.append(leg)
    if not chosen or not (MAIN_SLIP_ODDS_MIN <= total <= MAIN_SLIP_ODDS_MAX):
        return None
    return {
        "kind": "main",
        "target": MAIN_SLIP_ODDS_MIN,
        "total_odds": round(total, 2),
        "win_prob": round(_product_prob(chosen), 4),
        "legs": chosen,
    }


# ---- Slip codes -----------------------------------------------------------
# A slip is useless once you've closed the tab, so every slip gets a short code - five
# characters, letters and digits, the shape a bookmaker's app prints when you save a bet
# slip. Ours is minted here, not by 1xBet: 1xBet only accepts codes made inside 1xBet, so
# the code is a PredictSys one, resolvable against the published slips.
#
# Two codes are carried, because they answer different questions:
#   code          5 characters - what a person types or reads out; it points at a slip in
#                 this snapshot, so it is short but only resolves while that slip is listed.
#   code_payload  the whole slip packed into one longer string (PSB1-<base64url>). It
#                 carries the matches, picks and prices itself, so "Save slip" keeps one
#                 loadable on this device even after the data underneath has been rebuilt.
CODE_PREFIX = "PSB1-"
# 32 symbols: 0/O and 1/I are left out so a code read off a screen can't be mistyped.
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 5


def short_code(slip: dict, window_label: str, kind: str, salt: int = 0) -> str:
    """The 5-character code for one slip. Deterministic - the same slip always mints the
    same code - so a code a visitor copied keeps working until that slip leaves the feed."""
    legs = [[leg.get("match_key"), leg.get("selection"), leg.get("label")] for leg in slip["legs"]]
    seed = json.dumps([salt, window_label, kind, slip["total_odds"], legs], separators=(",", ":"))
    value = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
    out = []
    for _ in range(CODE_LENGTH):
        value, remainder = divmod(value, len(CODE_ALPHABET))
        out.append(CODE_ALPHABET[remainder])
    return "".join(out)


def assign_slip_codes(filters: list) -> None:
    """Give every slip in the snapshot its two codes, never handing out the same 5-character
    code twice (two slips sharing a code would make one of them unreachable)."""
    used = set()
    for entry in filters:
        label = entry.get("label") or ""
        slips_in_entry = ([("main", entry["main_slip"])] if entry.get("main_slip") else []) + [
            ("combo", slip) for slip in entry.get("slips") or []
        ]
        for kind, slip in slips_in_entry:
            salt = 0
            code = short_code(slip, label, kind)
            while code in used:
                salt += 1
                code = short_code(slip, label, kind, salt)
            used.add(code)
            slip["code"] = code
            slip["code_payload"] = encode_slip_code(slip, label, kind)
            slip["kind"] = kind
            slip["window"] = label


def encode_slip_code(slip: dict, window_label: str, kind: str) -> str:
    legs = []
    for leg in slip["legs"]:
        packed = {"m": leg["match_key"], "k": leg["kickoff_utc"], "l": leg["label"], "o": leg["odds"]}
        if leg.get("odds_source") == "1xlite":
            packed["x"] = 1  # only flag live prices; the model's fair odds are the default
        legs.append(packed)
    payload = {"v": 1, "w": window_label, "k": kind, "t": slip["total_odds"], "p": slip["win_prob"], "l": legs}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_slip_code(code: str) -> dict:
    """The inverse of encode_slip_code - what the page runs when a code is pasted back in.
    Raises ValueError with a readable message for anything that is not an intact code."""
    text = str(code or "").strip()
    if not text.startswith(CODE_PREFIX):
        raise ValueError(f"that is not a PredictSys bet code (they start with {CODE_PREFIX})")
    body = text[len(CODE_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:  # base64 and json both raise ValueError
        raise ValueError("the bet code is damaged - copy it again in full") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or not isinstance(payload.get("l"), list):
        raise ValueError("this bet code was made by a different version of the site")
    legs = []
    for packed in payload["l"]:
        whole = str(packed.get("m") or "")
        league, home, away = (whole.split("|", 2) + ["", "", ""])[:3]
        legs.append({
            "match_key": whole,
            "league": league or None,
            "home_team": home or "?",
            "away_team": away or "?",
            "kickoff_utc": packed.get("k"),
            "label": packed.get("l"),
            "odds": _num(packed.get("o")) or 0.0,
            "odds_source": "1xlite" if packed.get("x") else "model",
        })
    return {
        "window": payload.get("w"),
        "kind": payload.get("k") or "combo",
        "total_odds": _num(payload.get("t")) or 0.0,
        "win_prob": _num(payload.get("p")) or 0.0,
        "legs": legs,
    }


def build_filter(rows, prices: dict, now: dt.datetime, day_count: int) -> dict:
    """One filter's window: its eligible legs, the headline main slip, and up to three other
    combination slips inside 2.0-4.5."""
    start = day_start(now)
    end = start + dt.timedelta(days=day_count)
    legs, main_legs = [], []
    for row in rows:
        if (row.get("sport") or "football") == "football" and row.get("league") in NON_TOP_TIER_LEAGUES:
            continue  # second divisions aren't top-tier football - see NON_TOP_TIER_LEAGUES
        when = kickoff_of(row)
        if when is None or not (start <= when < end):
            continue
        price = prices.get(match_key(row))
        leg = best_leg(row, price)
        if leg is not None:
            legs.append(leg)
        main_leg = best_leg(row, price, MAIN_LEG_ODDS_MIN, MAIN_LEG_ODDS_MAX)
        if main_leg is not None:
            main_legs.append(main_leg)

    main_slip = build_main_slip(main_legs)
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
        "main_slip": main_slip,
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
    assign_slip_codes(filters)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day_start_hour": DAY_START_HOUR,
        "day_start_tz": DAY_TZ_NAME,
        "code_prefix": CODE_PREFIX,
        "code_length": CODE_LENGTH,
        "code_alphabet": CODE_ALPHABET,
        "leg_odds_range": [LEG_ODDS_MIN, LEG_ODDS_MAX],
        "slip_odds_range": [SLIP_ODDS_MIN, SLIP_ODDS_MAX],
        "main_leg_odds_range": [MAIN_LEG_ODDS_MIN, MAIN_LEG_ODDS_MAX],
        "main_slip_odds_range": [MAIN_SLIP_ODDS_MIN, MAIN_SLIP_ODDS_MAX],
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
