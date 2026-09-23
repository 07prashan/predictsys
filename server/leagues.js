// Mirrors fetch_data.py's LEAGUES dict on the Python side - kept here too since
// the database only stores the short codes (E0, SP1, ...), not display names.
// Shared by every route file so there's exactly one place to update on the Node
// side when a league is added or renamed.
const LEAGUE_NAMES = {
  E0: "Premier League (England)",
  E1: "Championship (England)",
  SP1: "La Liga (Spain)",
  SP2: "Segunda Division (Spain)",
  D1: "Bundesliga (Germany)",
  D2: "2. Bundesliga (Germany)",
  I1: "Serie A (Italy)",
  I2: "Serie B (Italy)",
  F1: "Ligue 1 (France)",
  F2: "Ligue 2 (France)",
  P1: "Primeira Liga (Portugal)",
  N1: "Eredivisie (Netherlands)",
  G1: "Super League (Greece)",
  T1: "Super Lig (Turkey)",
  B1: "Jupiler Pro League (Belgium)",
  SC0: "Premiership (Scotland)",
  SC1: "Championship (Scotland)",
};

// Both derived from LEAGUE_NAMES's "Competition Name (Country)" convention rather
// than keeping parallel maps that could drift out of sync with the source dict.
function countryOf(leagueCode) {
  const name = LEAGUE_NAMES[leagueCode];
  const match = name && name.match(/\(([^)]+)\)$/);
  return match ? match[1] : leagueCode;
}

// Same name with the "(Country)" suffix stripped - for display once the country's
// already shown as a heading above it (e.g. "Championship", not "Championship (England)").
function competitionOf(leagueCode) {
  const name = LEAGUE_NAMES[leagueCode];
  return name ? name.replace(/\s*\([^)]+\)$/, "") : leagueCode;
}

module.exports = { LEAGUE_NAMES, countryOf, competitionOf };
