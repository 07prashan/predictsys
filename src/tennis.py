"""ATP and WTA tennis: Elo ratings (overall + per-surface), match-winner probabilities,
and a set-score model for the sets markets, for the matches ESPN lists as upcoming.

Ratings follow FiveThirtyEight's tennis Elo: a K that shrinks as a player's match count
grows (so a newcomer's rating moves fast and a veteran's slowly), tracked separately overall
and per surface, with a prediction that blends the two - clay and grass specialists are real,
but a surface rating alone is built on too few matches to trust by itself.

History: Jeff Sackmann's tour-level match results (CC BY-NC-SA 4.0 - non-commercial use,
with attribution), read from the public archive mirror because the original repositories were
taken down. The mirror is a snapshot (currently ending at the 2026 Roland Garros), so the
weeks since then are backfilled from ESPN's finished matches; the backfill window shrinks on
its own if the mirror is ever refreshed.

A match's winner probability comes from the rating gap; the set-score distribution then
follows from it by assuming each set is an independent coin flip with whatever per-set win
probability reproduces that match probability. Sets in a real match aren't perfectly
independent, but it keeps every sets market consistent with the headline winner probability.

Usage (walk-forward validation of the settings below):
    python src/tennis.py
"""

import datetime as dt
import re
import unicodedata
from collections import defaultdict, deque
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sklearn.metrics import accuracy_score, log_loss

import espn

MIRROR_URL = "https://raw.githubusercontent.com/Aneeshers/tennis-sackmann-archive/main/{tour}/{tour}_matches_{year}.csv"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "tennis"

HISTORY_YEARS = 10  # Elo warm-up; ratings from further back than this have long since washed out
INITIAL_RATING = 1500.0
# A player with no history at all is almost always a wildcard or qualifier - weaker than the
# 1500 average a typical tour regular sits at.
UNKNOWN_PLAYER_RATING = 1400.0
LIMITED_DATA_MATCHES = 10  # below this many rated matches, a prediction is flagged as low-confidence
ACTIVE_WITHIN_DAYS = 400  # retired/long-injured players don't count towards the "rank among players"
BACKFILL_STEP_DAYS = 3  # shorter than any tournament, so every one is seen at least once
# The archive is a frozen snapshot, so the backfill window grows every day. Past weeks are cached
# on disk after their first fetch, which makes a long window cheap after one slow run - the cap
# only exists to stop a misconfigured run from fetching years of history.
MAX_BACKFILL_DAYS = 900
SURFACES = ("Hard", "Clay", "Grass")

# From evaluate() (fit on 2020-23, confirmed on 2024+ for both tours): a 30% surface blend
# beat overall-only and surface-only on log-loss, and the raw Elo gap needs shrinking in
# best-of-3 matches (it's overconfident there) but not in best-of-5, where the extra sets
# let the better player's edge show - the ratio between the two scales is about what
# independent sets would predict.
SURFACE_BLEND = 0.3
RATING_SCALE = {3: 0.75, 5: 1.05}

_ROUND_ORDER = {"R128": 0, "R64": 1, "R32": 2, "R16": 3, "QF": 4, "SF": 5, "F": 6, "RR": 2, "BR": 5, "ER": 0}

_GRASS = (
    "wimbledon", "halle", "queen", "eastbourne", "mallorca", "hertogenbosch", "libema", "newport", "hall of fame",
    "nottingham", "birmingham", "berlin", "bad homburg", "surbiton", "ilkley",
)
_CLAY = (
    "roland garros", "french open", "monte carlo", "monte-carlo", "madrid", "rome", "italian open", "barcelona",
    "hamburg", "geneva", "lyon", "estoril", "marrakech", "houston", "bucharest", "munich", "bmw open", "rabat",
    "strasbourg", "bastad", "båstad", "gstaad", "swiss open", "kitzbuhel", "kitzbühel", "umag", "croatia open",
    "bogota", "bogotá", "santiago", "chile open", "cordoba", "córdoba", "rio ", "rio open", "buenos aires",
    "argentina open", "sao paulo", "são paulo", "charleston", "parma", "prague", "warsaw", "palermo", "lausanne",
    "iasi", "iași", "budapest", "portoroz", "rouen", "belgrade", "serbia open", "cluj", "marbella", "lugano",
    "bad gastein", "oeiras", "tunis", "bari", "trnava",
)


def surface_of(tournament: str, when: pd.Timestamp) -> str:
    """ESPN doesn't say what a tournament is played on, so it's read off the name; only the
    handful of events that exist on two surfaces (Stuttgart) need the date as well."""
    name = tournament.lower()
    if "stuttgart" in name:
        return "Grass" if when.month in (6, 7) else "Clay"
    if any(token in name for token in _GRASS):
        return "Grass"
    if any(token in name for token in _CLAY):
        return "Clay"
    return "Hard"


def player_key(name: str) -> str:
    """One identity per player across both sources. Tokens are sorted because the two
    disagree on name order for many Asian players ('Zhou Yi' vs 'Yi Zhou')."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(sorted(re.sub(r"[^a-z ]", " ", folded).split()))


# ---- History -----------------------------------------------------------------------

_SET = re.compile(r"(\d+)-(\d+)(?:\(\d+\))?")


def _sets_won(score: str) -> tuple:
    """(winner's sets, loser's sets) from a score like '7-6(4) 3-6 6-2' or '6-4 3-1 RET'.
    A set only counts once it's actually finished - a retirement mid-set shouldn't hand
    the leader a set he never won."""
    won = lost = 0
    for a, b in _SET.findall(str(score)):
        a, b = int(a), int(b)
        done = (max(a, b) >= 6 and abs(a - b) >= 2) or (max(a, b) == 7 and min(a, b) in (5, 6))
        if done:
            won, lost = (won + 1, lost) if a > b else (won, lost + 1)
    return won, lost


def _load_mirror_year(tour: str, year: int) -> pd.DataFrame | None:
    path = RAW_DIR / f"{tour.lower()}_{year}.csv"
    if not path.exists():
        resp = requests.get(MIRROR_URL.format(tour=tour.lower(), year=year), timeout=60)
        if resp.status_code == 404:
            return None  # that year isn't in the archive
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return pd.read_csv(path, low_memory=False)


def load_history(tour: str, today: dt.date) -> pd.DataFrame:
    """Archive matches for one tour in the common shape used below. Davis Cup / Billie Jean
    King Cup ties (level D) and walkovers are dropped: neither is a normal tour match."""
    frames = []
    for year in range(today.year - HISTORY_YEARS, today.year + 1):
        df = _load_mirror_year(tour, year)
        if df is not None:
            frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw = raw[(raw["tourney_level"] != "D") & ~raw["score"].astype(str).str.contains("W/O|DEF", regex=True)]
    raw = raw.dropna(subset=["winner_name", "loser_name", "tourney_date"])

    start = pd.to_datetime(raw["tourney_date"].astype(int).astype(str), format="%Y%m%d")
    order = raw["round"].map(_ROUND_ORDER).fillna(3)
    sets = raw["score"].map(_sets_won)
    # the archive has some blank and some lower-cased surfaces ('clay'); a blank one is read
    # off the tournament name instead of throwing the match away
    surface = raw["surface"].astype("string").str.strip().str.title().replace({"Carpet": "Hard"})
    surface = [
        s if s in SURFACES else surface_of(name, when)
        for s, name, when in zip(surface, raw["tourney_name"], start)
    ]
    return pd.DataFrame(
        {
            "date": (start + pd.to_timedelta(order, unit="D")).to_numpy(),
            "tour": tour,
            "tournament": raw["tourney_name"].to_numpy(),
            "surface": surface,
            "best_of": raw["best_of"].fillna(3).astype(int).to_numpy(),
            "winner": raw["winner_name"].to_numpy(),
            "loser": raw["loser_name"].to_numpy(),
            "winner_sets": [s[0] for s in sets],
            "loser_sets": [s[1] for s in sets],
        }
    ).sort_values("date", kind="stable").reset_index(drop=True)


def espn_history(matches: pd.DataFrame, tour: str) -> pd.DataFrame:
    """ESPN's finished matches in the archive's shape. A retirement counts (the result is
    real, and the archive includes those too); a walkover doesn't (nothing was played)."""
    done = matches[(matches["state"] == "post") & (matches["winner"] > 0) & ~matches["status"].str.contains("WALKOVER", case=False)]
    done = done.reset_index(drop=True)
    if done.empty:
        return pd.DataFrame()
    first_won = done["winner"] == 1
    when = done["kickoff_utc"].dt.tz_convert(None)
    return pd.DataFrame(
        {
            "date": when.dt.normalize().to_numpy(),
            "tour": tour,
            "tournament": done["tournament"].to_numpy(),
            "surface": [surface_of(t, w) for t, w in zip(done["tournament"], when)],
            "best_of": np.where((tour == "ATP") & done["major"], 5, 3),
            "winner": np.where(first_won, done["p1"], done["p2"]),
            "loser": np.where(first_won, done["p2"], done["p1"]),
            "winner_sets": np.where(first_won, done["p1_sets"], done["p2_sets"]),
            "loser_sets": np.where(first_won, done["p2_sets"], done["p1_sets"]),
        }
    )


# ---- Elo ---------------------------------------------------------------------------


def _k(matches_played: int) -> float:
    return 250.0 / (matches_played + 5) ** 0.4


class Ratings:
    """Overall and per-surface Elo for every player, keyed by player_key()."""

    def __init__(self):
        self.overall = defaultdict(lambda: INITIAL_RATING)
        self.surface = {s: defaultdict(lambda: INITIAL_RATING) for s in SURFACES}
        self.played = defaultdict(int)
        self.played_surface = {s: defaultdict(int) for s in SURFACES}
        self.form = defaultdict(lambda: deque(maxlen=5))
        self.last_played = {}
        self.display = {}

    def known(self, key: str) -> bool:
        return key in self.played

    def rating(self, key: str, surface: str, blend: float = SURFACE_BLEND) -> float:
        if not self.known(key):
            return UNKNOWN_PLAYER_RATING
        return (1 - blend) * self.overall[key] + blend * self.surface[surface][key]

    def update(self, winner: str, loser: str, surface: str, when) -> None:
        for table, counts in ((self.overall, self.played), (self.surface[surface], self.played_surface[surface])):
            expected = 1.0 / (1.0 + 10 ** ((table[loser] - table[winner]) / 400.0))
            table[winner] += _k(counts[winner]) * (1.0 - expected)
            table[loser] -= _k(counts[loser]) * (1.0 - expected)
            counts[winner] += 1
            counts[loser] += 1
        self.form[winner].append("W")
        self.form[loser].append("L")
        self.last_played[winner] = self.last_played[loser] = when


def rate_matches(matches: pd.DataFrame, record: bool = False):
    """Chronological pass. With record=True also returns each match's PRE-match blended
    rating gap inputs, for evaluate() - read before the match updates anything, so they
    can't leak its own result."""
    ratings = Ratings()
    rows = []
    for m in matches.itertuples(index=False):
        w_key, l_key = player_key(m.winner), player_key(m.loser)
        ratings.display.setdefault(w_key, m.winner)
        ratings.display.setdefault(l_key, m.loser)
        if record:
            rows.append(
                (
                    ratings.overall[w_key], ratings.overall[l_key],
                    ratings.surface[m.surface][w_key], ratings.surface[m.surface][l_key],
                    ratings.played[w_key], ratings.played[l_key],
                )
            )
        ratings.update(w_key, l_key, m.surface, m.date)
    if not record:
        return ratings
    pre = pd.DataFrame(rows, columns=["ow", "ol", "sw", "sl", "nw", "nl"], index=matches.index)
    return ratings, pre


def win_probability(rating_a: float, rating_b: float, best_of: int) -> float:
    return 1.0 / (1.0 + 10 ** (-(rating_a - rating_b) * RATING_SCALE[best_of] / 400.0))


# ---- Set-score model ---------------------------------------------------------------


def _match_win_prob(s: float, best_of: int) -> float:
    need = best_of // 2 + 1
    return sum(comb(best_of, k) * s**k * (1 - s) ** (best_of - k) for k in range(need, best_of + 1))


def set_win_prob(p_match: float, best_of: int) -> float:
    """The per-set win probability s at which a best-of-N match is won with p_match
    (monotonic in s, so bisection finds it)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _match_win_prob(mid, best_of) < p_match else (lo, mid)
    return (lo + hi) / 2


def set_score_probs(p_match: float, best_of: int) -> dict:
    """{(sets_a, sets_b): probability} for every way the match can finish."""
    s = set_win_prob(p_match, best_of)
    need = best_of // 2 + 1
    dist = {}
    for loser_sets in range(need):
        # winner takes the last set, and exactly loser_sets of the earlier ones go the other way
        ways = comb(need - 1 + loser_sets, loser_sets)
        dist[(need, loser_sets)] = ways * s**need * (1 - s) ** loser_sets
        dist[(loser_sets, need)] = ways * (1 - s) ** need * s**loser_sets
    return dist


def sets_markets(p_match: float, best_of: int) -> dict:
    dist = set_score_probs(p_match, best_of)
    need = best_of // 2 + 1
    best = max(dist, key=dist.get)
    straight = sum(p for (a, b), p in dist.items() if min(a, b) == 0)
    distance = sum(p for (a, b), p in dist.items() if a + b == best_of)
    over_line = best_of - 0.5 if best_of == 3 else need + 0.5  # 2.5 sets in a best-of-3, 3.5 in a best-of-5
    over = sum(p for (a, b), p in dist.items() if a + b > over_line)
    return {
        "correct_score_home": best[0],
        "correct_score_away": best[1],
        "straight_sets_prob": straight,
        "goes_the_distance_prob": distance,
        "sets_line": over_line,
        "sets_over_prob": over,
    }


# ---- Fixtures ----------------------------------------------------------------------


def predict_fixtures(fixtures: pd.DataFrame, ratings: Ratings, tour: str) -> pd.DataFrame:
    rows = []
    for fx in fixtures.itertuples(index=False):
        when = fx.kickoff_utc.tz_convert(None)
        surface = surface_of(fx.tournament, when)
        best_of = 5 if (tour == "ATP" and fx.major) else 3
        k1, k2 = player_key(fx.p1), player_key(fx.p2)
        r1, r2 = ratings.rating(k1, surface), ratings.rating(k2, surface)
        p1 = win_probability(r1, r2, best_of)
        markets = sets_markets(p1, best_of)
        favourite_is_1 = p1 >= 0.5
        favourite = ratings.display.get(k1, fx.p1) if favourite_is_1 else ratings.display.get(k2, fx.p2)
        row = {
            "sport": "tennis",
            "league": tour,
            "competition": fx.tournament,
            "round": fx.round,
            "external_id": fx.external_id,
            "kickoff_utc": fx.kickoff_utc,
            "match_date": fx.kickoff_utc.strftime("%Y-%m-%d"),
            # the archive's spelling when we have it, so a player is one name everywhere
            "home_team": ratings.display.get(k1, fx.p1),
            "away_team": ratings.display.get(k2, fx.p2),
            "home_logo": fx.p1_flag,
            "away_logo": fx.p2_flag,
            "prob_home": p1,
            "prob_draw": 0.0,
            "prob_away": 1 - p1,
            "predicted_outcome": "H" if favourite_is_1 else "A",
            "best_pick_market": "Match Winner",
            "best_pick_label": f"{favourite} to win",
            "best_pick_prob": max(p1, 1 - p1),
            "home_form": "".join(ratings.form[k1]),
            "away_form": "".join(ratings.form[k2]),
            "home_rating": round(r1),
            "away_rating": round(r2),
            "surface": surface,
            "best_of": best_of,
            "grand_slam": int(bool(fx.major)),
            "limited_data": int(
                min(ratings.played.get(k1, 0), ratings.played.get(k2, 0)) < LIMITED_DATA_MATCHES
            ),
            **markets,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def new_since_snapshot(history: pd.DataFrame, finished: pd.DataFrame) -> pd.DataFrame:
    """ESPN's finished matches that the archive doesn't already have. The archive's last
    tournament can be there in part (a snapshot taken mid-event), so over the final weeks
    a match only counts as new if those two players don't already appear in the archive as
    winner and loser - cutting on date alone would double-count the overlap."""
    if finished.empty:
        return finished
    window_start = history["date"].max() - pd.Timedelta(days=21)
    recent = history[history["date"] >= window_start]
    seen = set(zip(recent["winner"].map(player_key), recent["loser"].map(player_key)))
    candidates = finished[finished["date"] >= window_start]
    is_new = [(player_key(w), player_key(l)) not in seen for w, l in zip(candidates["winner"], candidates["loser"])]
    return candidates[is_new]


def _backfill_days(history: pd.DataFrame, today: dt.date) -> list:
    wanted = history["date"].max().date() + dt.timedelta(days=1)
    earliest = today - dt.timedelta(days=MAX_BACKFILL_DAYS)
    if wanted < earliest:
        print(
            f"  (the tennis archive ends {wanted - dt.timedelta(days=1)}, over {MAX_BACKFILL_DAYS} days ago - results since then "
            "can't all be backfilled, so ratings will lag until the archive source is refreshed or replaced)"
        )
    start = max(wanted, earliest)
    days, day = [], start
    while day <= today:
        days.append(day)
        day += dt.timedelta(days=BACKFILL_STEP_DAYS)
    return days + [today]


def build(today: dt.date = None, horizon_days: int = 10) -> dict:
    """Everything the tennis side contributes to one prediction run, same shape as
    intl.build(): predictions, results (finished ESPN matches, for settling),
    matches (history for the head-to-head / profile pages), ratings."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    now = pd.Timestamp.now(tz="UTC")
    out = {"predictions": [], "results": [], "matches": [], "ratings": []}

    for tour in ("ATP", "WTA"):
        history = load_history(tour, today)
        recent_days = _backfill_days(history, today)
        upcoming_days = [today + dt.timedelta(days=d) for d in range(0, horizon_days + 1, 3)] + [today + dt.timedelta(days=horizon_days)]
        espn_matches = espn.fetch_tennis(tour, sorted(set(recent_days + upcoming_days)))

        backfill = new_since_snapshot(history, espn_history(espn_matches, tour)) if len(espn_matches) else pd.DataFrame()
        full = pd.concat([history, backfill], ignore_index=True).sort_values("date", kind="stable").reset_index(drop=True)
        ratings = rate_matches(full)
        print(
            f"{tour}: {len(full)} matches rated ({len(backfill)} backfilled from ESPN), "
            f"{len(ratings.played)} players"
        )

        if len(espn_matches):
            upcoming = espn_matches[
                (espn_matches["state"] == "pre")
                & (espn_matches["kickoff_utc"] >= now)
                & (espn_matches["kickoff_utc"] < now + pd.Timedelta(days=horizon_days))
            ]
            if len(upcoming):
                out["predictions"].append(predict_fixtures(upcoming, ratings, tour))
            out["results"].append(espn_matches[espn_matches["state"] == "post"])

        last = ratings.last_played
        cutoff = full["date"].max() - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)
        out["ratings"].append(
            pd.DataFrame(
                [
                    {"league": tour, "team": ratings.display[k], "elo_rating": ratings.overall[k]}
                    for k in ratings.played
                    if last[k] >= cutoff
                ]
            )
        )
        shown = full[full["date"] >= pd.Timestamp(today) - pd.DateOffset(years=6)]
        out["matches"].append(
            pd.DataFrame(
                {
                    "league": tour,
                    "date": shown["date"].dt.strftime("%Y-%m-%d").to_numpy(),
                    # the winner is stored on the "home" side, so a result is always 'H' - the
                    # head-to-head and team-profile pages already count that correctly, and the
                    # score columns carry sets won instead of goals
                    "home_team": [ratings.display[player_key(n)] for n in shown["winner"]],
                    "away_team": [ratings.display[player_key(n)] for n in shown["loser"]],
                    "home_goals": shown["winner_sets"].to_numpy(),
                    "away_goals": shown["loser_sets"].to_numpy(),
                    "result": "H",
                }
            )
        )

    return {
        "predictions": pd.concat(out["predictions"], ignore_index=True) if out["predictions"] else pd.DataFrame(),
        "results": pd.concat(out["results"], ignore_index=True) if out["results"] else pd.DataFrame(columns=espn.TENNIS_COLUMNS),
        "matches": pd.concat(out["matches"], ignore_index=True),
        "ratings": pd.concat(out["ratings"], ignore_index=True),
    }


def evaluate(first_test_year: int = 2024) -> None:
    """Walk-forward check of the Elo settings: one chronological pass gives every match its
    pre-match ratings, so scoring only needs the settings under test (surface blend, scale)
    to be fit on matches BEFORE the test period. Compares blends and scales on log-loss."""
    today = dt.datetime.now(dt.timezone.utc).date()
    for tour in ("ATP", "WTA"):
        history = load_history(tour, today)
        _, pre = rate_matches(history, record=True)
        experienced = (pre["nw"] >= 10) & (pre["nl"] >= 10)  # judge only matches with ratings worth something
        cutoff = pd.Timestamp(f"{first_test_year}-01-01")
        groups = {}  # (period, best_of) -> mask
        for period, in_period in (
            ("train", (history["date"] >= "2020-01-01") & (history["date"] < cutoff)),
            ("test", history["date"] >= cutoff),
        ):
            for best_of in sorted(history["best_of"].unique()):
                mask = in_period & experienced & (history["best_of"] == best_of)
                if mask.sum() >= 200:
                    groups[(period, best_of)] = mask

        print(f"\n{tour} (both players >=10 rated matches) - log-loss, lower is better; fit on 'train', confirm on 'test'")
        print(f"{'blend':>6}{'scale':>7}" + "".join(f"{p + ' bo' + str(b) + ' (n=' + str(int(m.sum())) + ')':>22}" for (p, b), m in groups.items()))
        for blend in (0.0, 0.3, 0.5, 0.7, 1.0):
            gap = (1 - blend) * (pre["ow"] - pre["ol"]) + blend * (pre["sw"] - pre["sl"])
            for scale in (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4):
                cells = []
                for mask in groups.values():
                    p = 1.0 / (1.0 + 10 ** (-gap[mask] * scale / 400.0))
                    ll = log_loss(np.ones(len(p)), np.column_stack([1 - p, p]), labels=[0, 1])
                    cells.append(f"{ll:>14.4f} ({accuracy_score(np.ones(len(p)), (p > 0.5).astype(int)):.3f})")
                print(f"{blend:>6.1f}{scale:>7.1f}" + "".join(f"{c:>22}" for c in cells))


if __name__ == "__main__":
    evaluate()
