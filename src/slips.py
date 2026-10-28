"""Filter slips: accumulators built from the day's most confident low-odds selections.

For every upcoming match the model already prices a whole board of markets. This module
turns that into a full 1xBet-style board per fixture - 1X2, double chance, totals, both-teams-
to-score, team totals, exact goals, handicaps and result+total for football (derived from the
model's own expected goals where the stored board has no such market), moneyline and totals
for basketball, match winner and sets markets for tennis, a match-winner board for anything
else (cricket once its rows exist) - then picks, per match, the selection the model is most
confident in whose decimal odds sit in a low-risk band, and combines them into slips.

Two hard rules keep a slip honest:
  * every leg must clear MIN_LEG_PROB (MAIN_LEG_PROB_MIN for the headline slip) - when the
    window holds no fixture the model likes that much, the slip is simply not built rather
    than padded with a weaker pick;
  * a window publishes ONE main slip plus up to MAX_SLIPS combination slips, each totalling
    between 2.00 and 3.00 (see _SLIP_PLAN) - five genuinely different combinations, not one
    slip with extra legs bolted on.

Legs come from the big leagues first (the popularity order the page shows) and, among the
smaller leagues, only from the clubs contesting Europe - the sides that keep finishing top of
their own league (see site_export.slip_eligible). A second division is never slip material.
Among legs the model likes about equally, the side that keeps winning is preferred (see
CONSISTENCY_WEIGHT) and the bigger competition ranks first.

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
import math
import os
import secrets
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

# ...and, above the odds band, a floor on the model's own probability. A real bookmaker's
# price can sit inside the odds band while the model rates the pick far shorter than that
# (the price carries the bookmaker's margin), so the odds alone never qualify a leg: this is
# the "genuinely very likely" test. Nothing below it is ever added to make a slip fit.
MIN_LEG_PROB = 0.70
MAIN_LEG_PROB_MIN = 0.75

# And the finished slip has to be worth placing, but not a lottery ticket: every combination
# slip lands in this window, so "odds of around 2-3" is a rule the builder cannot miss.
SLIP_ODDS_MIN = 2.00
SLIP_ODDS_MAX = 3.00
MAX_LEGS = 12  # a 12-leg 1.4 acca is already 56x; a slip this long is as far as we go
MAX_SLIPS = 5  # up to five combination slips beside the one headline main slip

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

# How many fixtures the page's slip editor offers for a window: every leg a slip was built
# from, plus the next-best fixtures to swap in (see build_filter). One shared pool, so the
# snapshot does not repeat the same fixture once per window.
POOL_LIMIT = 80
# Markets the editor offers alongside the leg-worthy ones: everything at least this likely,
# plus the best market on each shelf (so a draw or an under is always pickable).
POOL_MIN_PROB = 0.35

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

# Which shelf of a bookmaker's page each selection sits under - the editor groups its market
# picker by it, and the reason-text shown under a leg is written for the group.
_FOOTBALL_GROUP = {
    "1": "Result", "X": "Result", "2": "Result",
    "1X": "Double Chance", "12": "Double Chance", "X2": "Double Chance",
    "BTTS": "Both Teams to Score", "BTTS_NO": "Both Teams to Score",
}
for _code in ("O1.5", "U1.5", "O2.5", "U2.5", "O3.5", "U3.5"):
    _FOOTBALL_GROUP[_code] = "Totals"

# Markets this module derives from the model's expected goals rather than reading from the
# stored board - 1xBet offers these on every football game, and the model's own xG is enough
# to price them honestly. (Two 1xBet markets are deliberately absent: "team to qualify for
# the next stage" and "a team to win or draw a half" both need information the feed never
# carries - a tie's second leg and half-time results - so they are left out rather than faked.)
_DERIVED_FOOTBALL = [
    # code, market, label
    ("O0.5", "Goals", "At Least One Goal"),
    ("U0.5", "Goals", "No Goals"),
    ("O4.5", "Totals", "Over 4.5 Goals"),
    ("EX2", "Totals", "Exactly 2 Goals"),
    ("EX3", "Totals", "Exactly 3 Goals"),
    ("T1O1.5", "Team Totals", "Home Team Over 1.5 Goals"),
    ("T2O1.5", "Team Totals", "Away Team Over 1.5 Goals"),
    ("T1E2", "Team Totals", "Home Team Exactly 2 Goals"),
    ("T2E2", "Team Totals", "Away Team Exactly 2 Goals"),
    ("AH1", "Handicap", "Home -1.5 Handicap"),
    ("AH2", "Handicap", "Away -1.5 Handicap"),
    ("1+O2.5", "Result + Total", "Home Win & Over 2.5"),
    ("2+O2.5", "Result + Total", "Away Win & Over 2.5"),
]


def _pmf(lam: float, k: int) -> float:
    """Poisson P(X = k) without needing scipy: e^-lam * lam^k / k!."""
    return math.exp(-lam) * lam ** k / math.factorial(k)


def goal_matrix(lam_home: float, lam_away: float, cap: int = 12) -> list:
    """P(home scores h, away scores a) for h,a in 0..cap, from the model's expected goals.
    Independent Poisson - the truncated tail (beyond `cap` goals for one side, which is
    <1e-6 of the mass at the lambdas a fixture ever carries) is folded back onto the last
    cell so the matrix still sums to exactly 1."""
    lam_home = max(0.0, float(lam_home))
    lam_away = max(0.0, float(lam_away))
    home = [_pmf(lam_home, k) for k in range(cap + 1)]
    away = [_pmf(lam_away, k) for k in range(cap + 1)]
    tail_h = max(0.0, 1.0 - sum(home))
    tail_a = max(0.0, 1.0 - sum(away))
    home[-1] += tail_h
    away[-1] += tail_a
    return [[home[h] * away[a] for a in range(cap + 1)] for h in range(cap + 1)]


def derived_probabilities(row: dict) -> dict:
    """{selection code: probability} for every market priced from the row's expected goals,
    or {} when the row carries no xG (a row from before that column existed) - in which case
    the board simply stays the stored one."""
    lam_h, lam_a = _num(row.get("expected_goals_home")), _num(row.get("expected_goals_away"))
    if lam_h is None or lam_a is None:
        return {}
    grid = goal_matrix(lam_h, lam_a)
    size = len(grid)

    def cell(h, a):
        return grid[h][a] if 0 <= h < size and 0 <= a < size else 0.0

    total = [[cell(h, a) for a in range(size)] for h in range(size)]
    p_total = {}
    for h in range(size):
        for a in range(size):
            p_total[h + a] = p_total.get(h + a, 0.0) + cell(h, a)
    over = lambda line: sum(p for goals, p in p_total.items() if goals > line)  # noqa: E731
    out = {
        "O0.5": over(0.5), "U0.5": 1 - over(0.5), "O4.5": over(4.5),
        "EX2": p_total.get(2, 0.0), "EX3": p_total.get(3, 0.0),
        "T1O1.5": sum(cell(h, a) for h in range(size) for a in range(size) if h > 1.5),
        "T2O1.5": sum(cell(h, a) for h in range(size) for a in range(size) if a > 1.5),
        "T1E2": sum(cell(2, a) for a in range(size)),
        "T2E2": sum(cell(h, 2) for h in range(size)),
        "AH1": sum(cell(h, a) for h in range(size) for a in range(size) if h - a >= 2),
        "AH2": sum(cell(h, a) for h in range(size) for a in range(size) if a - h >= 2),
        "1+O2.5": sum(cell(h, a) for h in range(size) for a in range(size) if h > a and h + a > 2.5),
        "2+O2.5": sum(cell(h, a) for h in range(size) for a in range(size) if a > h and h + a > 2.5),
    }
    # the stored board (or the ensemble) already owns these lines when it has them
    for code, stored in (("O1.5", "over_1_5_prob"), ("O2.5", "over_2_5_prob"), ("O3.5", "over_3_5_prob")):
        value = _num(row.get(stored))
        if value is not None:
            out[code], out["U" + code[1:]] = value, 1 - value
    return {code: round(prob, 6) for code, prob in out.items() if 0 < prob < 1}


def _num(value):
    """A finite float, or None - guards against missing markets (a legacy row, a tennis
    row) and against NaN sneaking in from a partly-populated source."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _line_text(value) -> str:
    """A total's line as it appears on a betting page - 224.5 -> '224.5', 2.0 -> '2'."""
    number = _num(value)
    if number is None:
        return ""
    return str(int(number)) if number == int(number) else str(number)


def probability(row, code: str):
    """The model's probability for one selection code, or None when the row can't price it.
    Dispatches on the row's sport: football's full board, basketball's moneyline and the one
    total it prices, tennis's winner and sets markets, and a match-winner board for anything
    else - cricket, once its rows reach this database."""
    sport = row.get("sport") or "football"

    if sport == "tennis":
        if code in ("1", "2"):
            return _num(row.get("prob_home" if code == "1" else "prob_away"))
        if code == "DIST":
            return _num(row.get("goes_the_distance_prob"))
        if code.startswith("SETS_"):
            over = _num(row.get("sets_over_prob"))
            if over is None:
                return None
            return over if code.startswith("SETS_O") else 1 - over
        return None

    if sport == "basketball":
        if code in ("1", "2"):
            return _num(row.get("prob_home" if code == "1" else "prob_away"))
        if code.startswith(("O", "U")) and code[1:] == _line_text(row.get("total_line")):
            return _num(row.get("over_prob" if code.startswith("O") else "under_prob"))
        return None

    if sport != "football":
        # cricket and anything else the feed adds later: whoever the row's two sides are,
        # the win market prices, and so does a stored over/under line if one ever arrives
        if code in ("1", "2"):
            return _num(row.get("prob_home" if code == "1" else "prob_away"))
        if code.startswith(("O", "U")):
            over = _num(row.get(f"over_{code[1:].replace('.', '_')}_prob"))
            if over is None:
                return None
            return over if code.startswith("O") else 1 - over
        return None

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
    if code in _DERIVED_CODES:
        # a market the board derives from this row's expected goals (team totals, handicap,
        # result+total, ...) - computed on demand, so a row without xG prices none of them
        return derived_probabilities(row).get(code)
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


_DERIVED_CODES = {code for code, _market, _label in _DERIVED_FOOTBALL}

# Every football selection the board can show, stored and derived, in shelf order.
_FOOTBALL_BOARD = _FOOTBALL_SELECTIONS + _DERIVED_FOOTBALL


def selection_label(row, code: str) -> str:
    """What the leg says on the slip - the football labels come from the fixed table, while a
    winner leg in any other sport names the side or player it actually backs."""
    sport = row.get("sport") or "football"
    if sport == "tennis":
        if code in ("1", "2"):
            return f"{row.get('home_team') if code == '1' else row.get('away_team')} to win"
        return {"DIST": "Goes to a Deciding Set"}.get(code, code)
    if sport != "football":
        if code == "1":
            return f"{row.get('home_team')} to Win"
        if code == "2":
            return f"{row.get('away_team')} to Win"
        if code.startswith(("O", "U")):
            return f"{'Over' if code[0] == 'O' else 'Under'} {code[1:]} Points"
        return code
    return dict((c, label) for c, _market, label in _FOOTBALL_BOARD).get(code, code)


def selection_market(row, code: str) -> str:
    if (row.get("sport") or "football") == "tennis":
        return "Match Winner"
    return dict((c, market) for c, market, _label in _FOOTBALL_BOARD).get(code, "Market")


def selection_group(row, code: str) -> str:
    """The bookmaker shelf a selection sits on - the label the editor's market picker
    groups by, and the shelf its reason-text is written for. Football's shelves come from
    its own tables; every other sport names its shelves the way its page on a betting app
    does (a moneyline, not a football 'Result')."""
    sport = row.get("sport") or "football"
    if sport == "tennis":
        return "Match Winner" if code in ("1", "2") else "Sets"
    if sport == "basketball":
        if code in ("1", "2"):
            return "Moneyline"
        if code.startswith(("O", "U")):
            return "Total Points"
    if sport != "football":
        if code in ("1", "2"):
            return "Match Winner"
        if code.startswith(("O", "U")):
            return "Totals"
    return _FOOTBALL_GROUP.get(code) or _GROUPS_DERIVED.get(code, "Markets")


_GROUPS_DERIVED = {}
for _code, _market, _label in _DERIVED_FOOTBALL:
    _GROUPS_DERIVED[_code] = _market


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
    market that backs nobody (totals, both-teams-to-score, the draw). In every other sport
    the board's only side-backing selections are the winner markets - 1 is the home side,
    2 the away side - and their totals back nobody either."""
    if (row.get("sport") or "football") != "football":
        side = {"1": "home", "2": "away"}.get(code)
        if side is None:
            return None, None, None
        return row.get(f"{side}_team"), row.get(f"{side}_form"), row.get(f"{side}_rating")
    side = {"1": "home", "1X": "home", "2": "away", "X2": "away", "AH1": "home", "AH2": "away",
            "T1O1.5": "home", "T2O1.5": "away", "T1E2": "home", "T2E2": "away",
            "1+O2.5": "home", "2+O2.5": "away"}.get(code)
    if side is None:
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


def selections_for(row) -> list:
    """(code, market, label) for every market this fixture's sport offers on the page -
    football's whole board, basketball's moneyline and its priced total, tennis's winner and
    sets markets, and a match-winner board for anything else (cricket, once its rows exist)."""
    sport = row.get("sport") or "football"
    if sport == "football":
        return _FOOTBALL_BOARD
    if sport == "basketball":
        picks = [("1", "Moneyline", ""), ("2", "Moneyline", "")]
        line = _line_text(row.get("total_line"))
        if line and _num(row.get("over_prob")) is not None:
            picks += [
                (f"O{line}", "Total Points", f"Over {line} Points"),
                (f"U{line}", "Total Points", f"Under {line} Points"),
            ]
        return picks
    if sport == "tennis":
        picks = [("1", "Match Winner", ""), ("2", "Match Winner", "")]
        if _num(row.get("goes_the_distance_prob")) is not None:
            picks.append(("DIST", "Sets", "Goes to a Deciding Set"))
        line = _line_text(row.get("sets_line"))
        if line and _num(row.get("sets_over_prob")) is not None:
            picks += [
                (f"SETS_O{line}", "Sets", f"Over {line} Sets"),
                (f"SETS_U{line}", "Sets", f"Under {line} Sets"),
            ]
        return picks
    return [("1", "Match Winner", ""), ("2", "Match Winner", "")]


def board(row, prices: dict = None) -> list:
    """Every selection a bookmaker page would show for one fixture:
    [{code, market, label, group, prob, odds, odds_source}]. Real 1xLite prices win wherever
    that app quotes the code (it quotes football's 1X2, double chance, totals and BTTS) and a
    code it doesn't quote is dropped while prices are in use; everything else carries the
    model's fair price, 1 / probability. This list is also what the page's slip editor offers
    when you change a leg's market."""
    real = bool(prices)
    out = []
    for code, market, label in selections_for(row):
        prob = probability(row, code)
        if prob is None or not (0 < prob < 1):
            continue
        if real:
            odds = _num(prices.get(code))
            if odds is None:
                continue
            source = "1xlite"
        else:
            odds, source = 1.0 / prob, "model"
        out.append({
            "code": code,
            "market": market,
            "label": label or selection_label(row, code),
            "group": selection_group(row, code),
            "prob": round(prob, 4),
            "odds": round(odds, 3),
            "odds_source": source,
        })
    return out


def best_leg(row, prices: dict = None, lo: float = LEG_ODDS_MIN, hi: float = LEG_ODDS_MAX,
             min_prob: float = MIN_LEG_PROB):
    """The model's most likely selection whose odds fall in the band AND whose probability
    clears the floor - the single safest low-priced pick this match can offer. A price inside
    the band is not enough on its own (a bookmaker's margin can put a 60% pick at 1.40), so
    nothing below `min_prob` is ever used, and when no selection qualifies the fixture simply
    contributes no leg rather than a weaker one."""
    best = None
    for entry in board(row, prices):
        if entry["prob"] < min_prob or not (lo <= entry["odds"] <= hi):
            continue
        # highest probability wins; on a tie the stored market (earlier on the board) keeps
        # the leg, so an exotic derived price never displaces the canonical selection
        if best is None or entry["prob"] > best["prob"]:
            best = entry
    if best is None:
        return None
    backed_team, backed_form, backed_rating = backed_side(row, best["code"])
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
        "selection": best["code"],
        "market": best["market"],
        "label": best["label"],
        "group": best["group"],
        "prob": best["prob"],
        "odds": best["odds"],
        "odds_source": best["odds_source"],
        # where this fixture sits in the page's popularity order - the tie-break that keeps a
        # leg from a bigger competition ahead of an equal-priced one from a smaller league
        "rank": row.get("fixture_rank"),
        # whose form/rating this leg is actually backing (None for a totals/BTTS leg) -
        # leg_score uses the form, and the slip shows it for context
        "backed_team": backed_team,
        "backed_form": backed_form,
        "backed_rating": backed_rating,
    }


def _order(legs, strategy: str):
    if strategy == "safest":
        # probability first, then the consistency nudge to order near-ties, then the page's
        # popularity rank so an equal-priced leg from a bigger competition comes first
        return sorted(legs, key=lambda leg: (-round(leg_score(leg), 4), -leg["prob"], leg.get("rank") if leg.get("rank") is not None else 999, leg["odds"], leg["kickoff_utc"]))
    if strategy == "soonest":
        return sorted(legs, key=lambda leg: (leg["kickoff_utc"], -leg["prob"]))
    if strategy == "mixed":
        # one leg per competition in turn, so a slip is a spread of the day's fixtures rather
        # than five games from the same league - ranked by safety inside each round
        by_league = {}
        for leg in sorted(legs, key=lambda leg: (-round(leg_score(leg), 4), -leg["prob"])):
            by_league.setdefault(leg.get("league") or leg.get("competition") or "", []).append(leg)
        ordered, pools = [], list(by_league.values())
        while pools:
            remaining = []
            for pool in pools:
                ordered.append(pool.pop(0))
                if pool:
                    remaining.append(pool)
            pools = remaining
        return ordered
    if strategy == "value":
        # what the price is worth: probability x odds. Meaningful against a real bookmaker's
        # price (fair odds score exactly 1.0 for everyone)
        return sorted(legs, key=lambda leg: (-(leg["prob"] * leg["odds"]), -leg["prob"], leg["kickoff_utc"]))
    return sorted(legs, key=lambda leg: (-leg["odds"], -leg["prob"]))  # biggest-price legs first


# Each target is built with a different ordering, so the five slips on a filter are genuinely
# different combinations rather than one slip repeated with extra legs bolted on: the safest,
# the soonest, a spread across competitions, the best-priced, and the biggest total - all
# inside the 2.00-3.00 window every combination slip has to land in.
_SLIP_PLAN = [(2.0, "safest"), (2.3, "soonest"), (2.5, "mixed"), (2.7, "value"), (3.0, "biggest")]


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
        "strategy": strategy,
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
    1.10-1.30 and rated at least MAIN_LEG_PROB_MIN, stacked until the slip pays at least 2.00
    and never more than 3.50. Legs are taken most-likely-first - with the consistency nudge
    ordering near-ties, the same way the "safest" combination slip is ordered - so the
    finished slip is as safe as it can be. When the window holds no fixture that strong, there
    is no main slip; a weaker one is never invented to fill the slot."""
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
#                 One per slip per betting day: DailyCodes reuses the same code all day and
#                 the whole board mints fresh ones at 4am Kathmandu.
#   code_payload  the whole slip packed into one longer string (PSB1-<base64url>). It
#                 carries the matches, picks and prices itself, so "Save slip" keeps one
#                 loadable on this device even after the data underneath has been rebuilt.
CODE_PREFIX = "PSB1-"
# 32 symbols: 0/O and 1/I are left out so a code read off a screen can't be mistyped.
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 5


def day_bucket(now: dt.datetime) -> int:
    """Which betting day `now` belongs to - an integer that changes at 4am Kathmandu, the
    same boundary day_start() draws. Everything the code system says is "today" keys off
    this number, so the page and the feed can never disagree about which day it is."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local = now.astimezone(DAY_TZ) - dt.timedelta(hours=DAY_START_HOUR)
    return local.date().toordinal()


def mint_code(taken: set) -> str:
    """A random 5-character code none of `taken` already uses - bookmaker-style, carrying no
    information about the slip, so re-pricing a slip can never change its code."""
    for _ in range(1000):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in taken:
            return code
    raise RuntimeError("could not mint a unique slip code")


class DailyCodes:
    """One code per slip per betting day, kept in the prediction database.

    A code a visitor copies has to keep naming the same slip for the whole day, so it cannot
    be derived from the slip's contents - the moment 1xLite moves or the model refits, the
    contents change and a content-hashed code would silently stop resolving. Instead each
    slip's *slot* (its window plus its position in that window) is mapped to a random code
    the first time a build sees it on a given day, and every later build that day reads the
    same code back out of `slip_codes` - whatever the slip now holds. When the betting day
    rolls over at 4am Kathmandu the bucket changes, so the whole board mints fresh codes and
    yesterday's rows are dropped.

    The table travels with data/predictions.db, which scripts/ci_state.sh already packs and
    restores between CI runs - so the mapping survives the empty disk of a hosted runner.
    """

    def __init__(self, conn, now: dt.datetime = None):
        self.conn = conn
        self.bucket = day_bucket(now or dt.datetime.now(dt.timezone.utc))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS slip_codes ("
            " day_bucket INTEGER NOT NULL,"
            " slot TEXT NOT NULL,"
            " code TEXT NOT NULL,"
            " minted_at TEXT NOT NULL,"
            " PRIMARY KEY (day_bucket, slot)"
            ")"
        )
        conn.execute("DELETE FROM slip_codes WHERE day_bucket < ?", (self.bucket,))
        conn.commit()

    def code_for(self, slot: str) -> str:
        row = self.conn.execute(
            "SELECT code FROM slip_codes WHERE day_bucket = ? AND slot = ?", (self.bucket, slot)
        ).fetchone()
        if row:
            return row[0]
        used = {r[0] for r in self.conn.execute("SELECT code FROM slip_codes WHERE day_bucket = ?", (self.bucket,))}
        code = mint_code(used)
        self.conn.execute(
            "INSERT INTO slip_codes (day_bucket, slot, code, minted_at) VALUES (?, ?, ?, ?)",
            (self.bucket, slot, code, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        self.conn.commit()
        return code


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


def assign_slip_codes(filters: list, codes=None) -> None:
    """Give every slip in the snapshot its two codes, never handing out the same 5-character
    code twice (two slips sharing a code would make one of them unreachable).

    With `codes` (the site's DailyCodes) a slip keeps one code for the whole betting day -
    see that class. Without it (the standalone tool, the tests) codes are minted
    deterministically from the slip itself, which is only good while the slip is unchanged."""
    used = set()
    for entry in filters:
        label = entry.get("label") or ""
        slips_in_entry = ([("main", entry["main_slip"])] if entry.get("main_slip") else []) + [
            ("combo", slip) for slip in entry.get("slips") or []
        ]
        for position, (kind, slip) in enumerate(slips_in_entry):
            if codes is not None:
                code = codes.code_for(f"{label}|{kind}|{position}")
            else:
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


# ---- Why a leg is on the slip -------------------------------------------
# Every leg on a published slip carries one line of evidence, written from what the database
# actually holds about the fixture: the two sides' last five games (goals scored in, both-
# teams-scored games, over-2.5 games, the win run) and their last meeting. Nothing is written
# about a team the database has no games for - that leg falls back to the model's own number.

def eligible(row: dict) -> bool:
    """Whether one fixture may contribute a leg at all. site_export tags every row (see
    slip_eligible): second divisions are always out, and among the smaller leagues only the
    clubs contesting Europe qualify. A snapshot built without that tag - the standalone tool,
    a test - falls back to today's rule: every top flight, no second divisions."""
    flag = row.get("slip_eligible")
    if flag is not None:
        return bool(flag)
    if (row.get("sport") or "football") != "football":
        return True
    return row.get("league") not in NON_TOP_TIER_LEAGUES


def _recent(context: dict, team: str) -> list:
    return ((context or {}).get("teams") or {}).get(team) or []


def _stats(matches: list) -> dict | None:
    """The counts a reason-line quotes from one side's last few games: played, won, scored
    in, both-scores games, over-2.5 games, games won by two clear goals, and the form run."""
    recent = list(matches or [])[:FORM_LENGTH]
    if not recent:
        return None
    out = {"n": len(recent), "wins": 0, "scored": 0, "both": 0, "over25": 0, "big": 0, "goals": 0.0}
    out["over"] = {0.5: 0, 1.5: 0, 2.5: 0, 3.5: 0}
    out["exact"] = {2: 0, 3: 0}
    for game in recent:
        gf = _num(game.get("goals_for")) or 0.0
        ga = _num(game.get("goals_against")) or 0.0
        total = gf + ga
        won = game.get("outcome") == "W"
        out["wins"] += int(won)
        if gf > 0:
            out["scored"] += 1
            out["both"] += int(ga > 0)
        out["over25"] += int(total > 2.5)
        for line in out["over"]:
            out["over"][line] += int(total > line)
        for n in out["exact"]:
            out["exact"][n] += int(total == n)
        out["big"] += int(won and gf - ga >= 2)
        out["goals"] += gf
    out["avg"] = out["goals"] / out["n"]
    # the feed lists newest first; a form run reads oldest -> newest (see form_wins)
    out["run"] = "".join(str(game.get("outcome") or "") for game in reversed(recent))
    return out


def _meeting(context: dict, match_key_: str):
    """The last time these two met, as (home, away, home_goals, away_goals) - None when the
    database has never logged the pairing."""
    meetings = ((context or {}).get("h2h") or {}).get(match_key_) or {}
    last = (meetings.get("meetings") or [None])[0]
    if not last:
        return None
    hg, ag = _num(last.get("home_goals")), _num(last.get("away_goals"))
    if hg is None or ag is None:
        return None
    return last.get("home_team"), last.get("away_team"), hg, ag


def _total_line_text(row: dict, leg: dict) -> str:
    """What a totals leg in a sport without goal history can honestly say: the model's
    projection against the line, and how likely this side of it is."""
    predicted = _num(row.get("predicted_total"))
    line = _num(row.get("total_line"))
    pct_text = f"{leg['prob'] * 100:.0f}%"
    if predicted is None or line is None:
        return f"The model rates {leg['label']} {pct_text} likely ({leg['odds']:.2f} fair odds)."
    return (f"The model projects {predicted:.0f} points against a {line:g} line, so "
            f"{leg['label']} is rated {pct_text} likely.")


def insight_for(row: dict, leg: dict, context: dict = None) -> str:
    """One line saying WHY this market was picked for this fixture, built from the fixture's
    own record - the two sides' recent scoring, the win run of the side the leg backs, and
    the last meeting - plus the model's number. The same text for a market wherever it
    appears, so switching a leg's market in the editor switches the reason with it."""
    home, away = row.get("home_team"), row.get("away_team")
    h, a = _stats(_recent(context, home)), _stats(_recent(context, away))
    meeting = _meeting(context, leg.get("match_key") or "")
    meeting_line = ""
    if meeting:
        m_home, m_away, hg, ag = meeting
        meeting_line = f" Their last meeting finished {hg:.0f}-{ag:.0f} ({m_home} v {m_away})."
    pct_text = f"{leg['prob'] * 100:.0f}%"
    group = leg.get("group") or ""

    if group == "Both Teams to Score":
        if h and a:
            return (f"Both teams scored in {h['both']} of {home}'s last {h['n']} and "
                    f"{a['both']} of {away}'s last {a['n']}.{meeting_line}")
    elif group in ("Totals", "Goals"):
        # "Goals" is the shelf At Least One Goal / No Goals sit on - priced the same way
        if not (h and a):
            pass  # no history for one of the sides - the model's number is written below
        else:
            selection = leg.get("selection") or ""
            line = selection[1:] if selection[:1] in ("O", "U") else ""
            if line in ("0.5", "1.5", "2.5", "3.5"):
                if line == "0.5" and selection[0] == "O":
                    return (f"{home} scored in {h['scored']} of their last {h['n']} and {away} scored "
                            f"in {a['scored']} of their last {a['n']}.{meeting_line}")
                if line == "0.5":
                    return (f"Neither side scored in {h['n'] - h['over'][0.5]} of {home}'s last {h['n']} "
                            f"and {a['n'] - a['over'][0.5]} of {away}'s last {a['n']}.{meeting_line}")
                key, over = float(line), selection[0] == "O"
                h_hits = h["over"][key] if over else h["n"] - h["over"][key]
                a_hits = a["over"][key] if over else a["n"] - a["over"][key]
                word = "Over" if over else "Under"
                return (f"{word} {line} landed in {h_hits} of {home}'s last {h['n']} and "
                        f"{a_hits} of {away}'s last {a['n']}.{meeting_line}")
            if selection.startswith("EX") and selection[2:].isdigit():
                n = int(selection[2:])
                if n in h["exact"] and n in a["exact"]:
                    return (f"Exactly {n} goals came up in {h['exact'][n]} of {home}'s last {h['n']} "
                            f"and {a['exact'][n]} of {away}'s last {a['n']}.{meeting_line}")
            return (f"{h['over25']} of {home}'s last {h['n']} and {a['over25']} of {away}'s last "
                    f"{a['n']} went over 2.5 goals.{meeting_line}")
    elif group in ("Result", "Double Chance", "Result + Total"):
        backed, form, _rating = backed_side(row, leg.get("selection") or "")
        stats = _stats(_recent(context, backed)) if backed else None
        if stats:
            line = (f"{backed} have won {stats['wins']} of their last {stats['n']} "
                    f"({stats['run']}) and scored in {stats['scored']} of them")
            if group == "Result + Total":
                line += f"; {stats['over25']} of those games went over 2.5"
            return f"{line}.{meeting_line} The model has {leg['label']} at {pct_text}."
    elif group == "Team Totals":
        team = leg.get("backed_team") or home
        stats = _stats(_recent(context, team))
        if stats:
            return (f"{team} scored in {stats['scored']} of their last {stats['n']} games, "
                    f"{stats['avg']:.1f} goals a game.{meeting_line}")
    elif group == "Handicap":
        backed = leg.get("backed_team") or home
        stats = _stats(_recent(context, backed))
        if stats:
            lam_h = _num(row.get("expected_goals_home"))
            lam_a = _num(row.get("expected_goals_away"))
            lam_line = f" {lam_h:.1f}-{lam_a:.1f}" if lam_h is not None and lam_a is not None else ""
            return (f"{backed} have won by two or more goals in {stats['big']} of their last "
                    f"{stats['n']} games.{meeting_line} The model's expected goals are{lam_line}.")
    elif group in ("Match Winner", "Moneyline", "Sets", "Total Points"):
        # the reason follows the side this leg actually backs - an away win quotes the away
        # side's record, not the home side's - and is phrased so a single player reads right
        # beside a basketball team.
        backed = leg.get("backed_team") or backed_side(row, leg.get("selection") or "")[0]
        stats = _stats(_recent(context, backed)) if backed else None
        if stats:
            return f"{backed} won {stats['wins']} of their last {stats['n']} ({stats['run']}). {leg['label']} is rated {pct_text} likely."
        if group == "Total Points":
            # a basketball total: no per-game history in the feed, so the projection speaks
            return _total_line_text(row, leg)
        if group == "Sets":
            # a sets line backs nobody - quote both players' recent records instead
            if h and a:
                return (f"{home} won {h['wins']} of their last {h['n']}, {away} won {a['wins']} of "
                        f"their last {a['n']}. {leg['label']} is rated {pct_text} likely.")
        return f"The model rates {leg['label']} {pct_text} likely ({leg['odds']:.2f} fair odds)."

    backed_stats = _stats(_recent(context, leg.get("backed_team"))) if leg.get("backed_team") else None
    form_line = (f" {leg['backed_team']} have won {backed_stats['wins']} of their last {backed_stats['n']}."
                 if backed_stats else "")
    return f"The model has {leg['label']} at {pct_text} (fair odds {leg['odds']:.2f}).{form_line}"


def pool_entry(row: dict, entries: list, context: dict = None) -> dict:
    """One fixture as the slip editor offers it: its whole board, plus one reason-line per
    market group (so changing a leg's market changes its reason too)."""
    representative = {}
    for entry in entries:
        representative.setdefault(entry["group"], entry)
    key = "|".join(str(part) for part in match_key(row))
    insights = {
        group: insight_for(row, {"match_key": key, "label": pick["label"], "group": group,
                                 "prob": pick["prob"], "odds": pick["odds"],
                                 "selection": pick["code"]}, context)
        for group, pick in representative.items()
    }
    return {
        "match_key": key,
        "league": row.get("league"),
        "competition": row.get("competition") or row.get("league"),
        "sport": row.get("sport") or "football",
        "kickoff_utc": (kickoff_of(row) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team": row.get("home_team"),
        "away_team": row.get("away_team"),
        "home_logo": row.get("home_logo"),
        "away_logo": row.get("away_logo"),
        "rank": row.get("fixture_rank"),
        "markets": entries,
        "insights": insights,
    }


def build_filter(rows, prices: dict, now: dt.datetime, day_count: int, context: dict = None) -> dict:
    """One filter's window: its eligible legs, the headline main slip, and up to MAX_SLIPS
    other combination slips inside 2.0-3.0."""
    start = day_start(now)
    end = start + dt.timedelta(days=day_count)
    legs, main_legs = [], []
    for row in rows:
        if not eligible(row):
            continue  # second divisions, and (when we know it) small-league clubs outside Europe
        when = kickoff_of(row)
        if when is None or not (start <= when < end):
            continue
        price = prices.get(match_key(row))
        leg = best_leg(row, price)
        if leg is not None:
            leg["insight"] = insight_for(row, leg, context)
            legs.append(leg)
        main_leg = best_leg(row, price, MAIN_LEG_ODDS_MIN, MAIN_LEG_ODDS_MAX, MAIN_LEG_PROB_MIN)
        if main_leg is not None:
            main_leg["insight"] = insight_for(row, main_leg, context)
            main_legs.append(main_leg)

    main_slip = build_main_slip(main_legs)
    slips, seen = [], set()
    for target, strategy in _SLIP_PLAN:
        slip = build_slip(legs, target, strategy)
        if slip is None:
            continue
        # the same set of fixtures in a different order is the same bet twice - five
        # strategies only ever produce five genuinely different combinations
        signature = frozenset(leg["match_key"] for leg in slip["legs"])
        if signature in seen:
            continue
        seen.add(signature)
        slips.append(slip)
    slips.sort(key=lambda slip: slip["total_odds"])
    if len(slips) > MAX_SLIPS:
        slips = slips[:MAX_SLIPS]

    return {
        "id": None,
        "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_available": len(legs),
        "main_slip": main_slip,
        "slips": slips,
    }


def _pool_markets(entries: list) -> list:
    """The markets the editor keeps for one fixture: everything reasonably likely, and always
    the best market on every shelf - so Result, Double Chance, Totals, BTTS, Team Totals,
    Handicap and Result+Total each stay pickable even when nobody backs them at 70%."""
    best_of_group = {}
    for entry in entries:
        if entry["prob"] > best_of_group.get(entry["group"], {}).get("prob", -1):
            best_of_group[entry["group"]] = entry
    keep = {id(entry) for entry in best_of_group.values()}
    keep |= {id(entry) for entry in entries if entry["prob"] >= POOL_MIN_PROB}
    return [entry for entry in entries if id(entry) in keep]


def build_pool(rows, prices: dict, now: dt.datetime, context: dict, filters: list,
               limit: int = POOL_LIMIT) -> list:
    """Every fixture the page's slip editor can add, in one shared ranked list (big
    competitions first) carrying each fixture's whole board and its reason-lines. Fixtures a
    published slip already uses are always included, so every leg on screen can be edited."""
    start = day_start(now)
    end = start + dt.timedelta(days=max(day_count for _id, day_count, _label in FILTER_DAYS))
    required = set()
    for entry in filters:
        for slip in ([entry["main_slip"]] if entry.get("main_slip") else []) + list(entry.get("slips") or []):
            required.update(leg["match_key"] for leg in slip["legs"])

    candidates = []
    for row in rows:
        if not eligible(row):
            continue
        when = kickoff_of(row)
        if when is None or not (start <= when < end):
            continue
        entries = board(row, prices.get(match_key(row)))
        if not entries:
            continue
        rank = row.get("fixture_rank")
        candidates.append(("|".join(str(p) for p in match_key(row)) in required,
                           rank if rank is not None else 999, -max(e["prob"] for e in entries), row, entries))
    candidates.sort(key=lambda item: (not item[0], item[1], item[2]))
    return [pool_entry(row, _pool_markets(entries), context) for _req, _rank, _neg, row, entries in candidates[:limit]]


def build_slips(rows, now: dt.datetime = None, odds: dict = None, context: dict = None,
                 codes=None) -> dict:
    """The whole snapshot: `rows` is whatever the site would show as upcoming, `odds` is
    {(league, home_team, away_team): {code: price}} from xlite (optional), and `context` is
    the recent-results/head-to-head evidence the reason-lines are written from (optional -
    without it a leg still gets the model's own number as its reason). `codes` is the
    DailyCodes register that keeps one code per slip for the whole betting day (optional -
    without it codes are minted from the slip's own contents)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prices = odds or {}
    filters = []
    for filter_id, day_count, label in FILTER_DAYS:
        entry = build_filter(rows, prices, now, day_count, context)
        entry["id"] = filter_id
        entry["label"] = label
        entry["day_count"] = day_count
        filters.append(entry)
    assign_slip_codes(filters, codes)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day_start_hour": DAY_START_HOUR,
        "day_start_tz": DAY_TZ_NAME,
        "code_prefix": CODE_PREFIX,
        "code_length": CODE_LENGTH,
        "code_alphabet": CODE_ALPHABET,
        "leg_odds_range": [LEG_ODDS_MIN, LEG_ODDS_MAX],
        "min_leg_prob": MIN_LEG_PROB,
        "slip_odds_range": [SLIP_ODDS_MIN, SLIP_ODDS_MAX],
        "max_slips": MAX_SLIPS,
        "main_leg_odds_range": [MAIN_LEG_ODDS_MIN, MAIN_LEG_ODDS_MAX],
        "main_slip_odds_range": [MAIN_SLIP_ODDS_MIN, MAIN_SLIP_ODDS_MAX],
        "n_matches_with_real_odds": len(prices),
        "pool": build_pool(rows, prices, now, context, filters),
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

    now = dt.datetime.now(dt.timezone.utc)
    conn = storage.connect()
    try:
        rows = site_export.upcoming(conn, site_export.Logos(conn), now)
        odds = {}
        try:
            import xlite

            odds = xlite.odds_for_rows(rows)
        except Exception as exc:  # noqa: BLE001 - the standalone tool works offline too
            print(f"  (live odds unavailable - using model prices: {exc})")
        payload = build_slips(rows, now=now, odds=odds, codes=DailyCodes(conn, now))
    finally:
        conn.close()
    path = write_slips(payload)
    print(f"{sum(len(f['slips']) for f in payload['filters'])} slips written -> {path}")


if __name__ == "__main__":
    main()
