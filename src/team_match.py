"""Maps ESPN's club names onto the football-data.co.uk names the models are keyed on
('Manchester United' -> 'Man United', 'Internazionale' -> 'Inter').

The models' ratings, form trackers and Poisson fits are all keyed by football-data's
spelling, so a fixture whose teams can't be resolved to it can't be predicted at all -
and one resolved to the WRONG team would be a confident prediction about somebody else.
So matching is strict: the explicit alias table wins, then accent-folded token matching
that has to find exactly one candidate in the league; anything ambiguous or unknown is
reported and left out rather than guessed.
"""

import re
import unicodedata

# ESPN -> football-data, for the names token matching can't be trusted with (abbreviations
# that aren't a prefix of the full word, translations, and one club's several spellings).
CLUB_ALIASES = {
    "Nottingham Forest": "Nott'm Forest",
    "Queens Park Rangers": "QPR",
    "Wolverhampton Wanderers": "Wolves",
    "Borussia Mönchengladbach": "M'gladbach",
    "FC Cologne": "FC Koln",
    "Athletic Club": "Ath Bilbao",
    "Atlético Madrid": "Ath Madrid",  # football-data still spells it the old "Athletic Madrid" way
    "Deportivo": "La Coruna",
    "Espanyol": "Espanol",
    "RC Celta Fortuna": "Celta B",
    "Real Sociedad II": "Sociedad B",
    "Paris Saint-Germain": "Paris SG",
    "Stade Rennais": "Rennes",
    "Saint-Étienne": "St Etienne",
    "Sporting CP": "Sp Lisbon",
    "Braga": "Sp Braga",
    "Istanbul Basaksehir": "Buyuksehyr",
    "OH Leuven": "Oud-Heverlee Leuven",
    "Sint-Truidense": "St Truiden",
    "Levadiakos": "Levadeiakos",
    "Olympiacos": "Olympiakos",
    "Heart of Midlothian": "Hearts",
    "Raith Rovers": "Raith Rvs",
    "Sporting Gijón": "Sp Gijon",
    # World leagues (ESPN -> football-data's spelling). Token matching gets most of these
    # right on its own; these are the ones verified by comparing both sources' actual name
    # sets for 2024-2026 - every other name in those leagues already resolved verbatim.
    "Argentinos Juniors": "Argentinos Jrs",
    "Estudiantes de La Plata": "Estudiantes L.P.",  # not plain 'Estudiantes' - that's a different club
    "Gimnasia La Plata": "Gimnasia L.P.",
    "Independiente Rivadavia": "Ind. Rivadavia",  # not 'Independiente' - Mendoza, not Avellaneda
    "San Martín (San Juan)": "San Martin S.J.",
    "Santos": "Santos Laguna",
    "Atlanta United FC": "Atlanta Utd",
    "D.C. United": "DC United",
    "Inter Miami CF": "Inter Miami",
    "LA Galaxy": "Los Angeles Galaxy",
    "LAFC": "Los Angeles FC",
    "Tokyo Verdy 1969": "Verdy",
    "América": "Club America",
    "São Paulo": "Sao Paulo",
    "Talleres (Córdoba)": "Talleres Cordoba",
    "Belgrano (Córdoba)": "Belgrano",
    "Central Córdoba (Santiago del Estero)": "Central Cordoba",
    "Unión (Santa Fe)": "Union de Santa Fe",
    "Godoy Cruz Antonio Tomba": "Godoy Cruz",
    # Big European clubs whose ESPN name out-tokenizes the football-data one ('Inter Milan'
    # also covers 'AC Milan', so token matching can't choose between them).
    "FC Porto": "Porto",
    "Internazionale": "Inter",
    "Inter Milan": "Inter",
    "NEC Nijmegen": "Nijmegen",
    "Vitória de Guimaraes": "Guimaraes",
    "Shanghai Port FC": "Shanghai Port",
    "West Ham United": "West Ham",
    # Denmark and Russia (the two leagues added with the Nordics) - the ones ESPN spells
    # differently enough that token matching finds no single candidate.
    "AGF": "Aarhus",
    "Brøndby IF": "Brondby",
    "F.C. København": "FC Copenhagen",
    "FC Nordsjælland": "Nordsjaelland",
    "Sønderjyske Fodbold": "Sonderjyske",
    "Akron Tolyatti": "Akron Togliatti",
    "Dinamo Moscow": "Dynamo Moscow",
    "Krylia Sovetov": "Krylya Sovetov",
}

# Tokens that say what KIND of club it is rather than which one: 'FC Porto' and 'Porto',
# 'AFC Bournemouth' and 'Bournemouth' are the same team.
_GENERIC = {
    "fc", "afc", "cf", "sc", "sv", "vfb", "vfl", "tsg", "fk", "sk", "ac", "as", "aj", "rc", "cd", "c", "d", "ud",
    "sd", "ca", "us", "ss", "ssc", "kv", "kaa", "kvc", "ksv", "ksc", "ofi", "de", "the", "1", "1899", "05", "04", "07", "96",
}


def _tokens(name: str) -> list:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9 ]", " ", folded.replace("'", "")).split()
    kept = [w for w in words if w not in _GENERIC]
    return kept or words


def _token_match(a: str, b: str) -> bool:
    """Equal, or one is a prefix of the other ('Man' / 'Manchester'). Prefixes under 3
    letters don't count: 'St' would otherwise match 'Stuttgart' and 'Standard', 'Le' would
    match 'Lens'."""
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def _covers(shorter: list, longer: list) -> bool:
    """Every token of the shorter name is matched by some (distinct) token of the longer one."""
    pool = list(longer)
    for token in shorter:
        hit = next((p for p in pool if _token_match(token, p)), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def match_club(espn_name: str, known: set) -> str | None:
    """The football-data spelling for `espn_name` among `known` (one league's team names),
    or None if it isn't there or is ambiguous."""
    aliased = CLUB_ALIASES.get(espn_name, espn_name)
    if aliased in known:
        return aliased

    wanted = _tokens(aliased)
    candidates = []
    for name in known:
        theirs = _tokens(name)
        shorter, longer = (wanted, theirs) if len(wanted) <= len(theirs) else (theirs, wanted)
        if _covers(shorter, longer):
            candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def match_clubs(espn_names: set, current: set, all_time: set = None) -> tuple:
    """({espn_name: football-data name}, [espn names that couldn't be resolved]).
    `current` is the league's team names this season, tried first - football-data has
    respelled some clubs over the years, so matching against every name ever used could
    find two candidates for one team. `all_time` is only the fallback, for a club that
    hasn't played a match yet this season."""
    resolved, unresolved = {}, []
    for name in sorted(espn_names):
        hit = match_club(name, current) or (match_club(name, all_time) if all_time else None)
        if hit is None:
            unresolved.append(name)
        else:
            resolved[name] = hit
    return resolved, unresolved
