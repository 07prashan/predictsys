// Maps football-data.co.uk's abbreviated team names (what's stored in the database)
// to a fuller name that searches reliably against TheSportsDB's logo API.
//
// The base of this is the same mapping src/xg.py built and verified (100% match
// rate) against Understat - reused here in reverse, since Understat's names are
// already the fuller, more "proper" form these searches need.
//
// A few names still needed manual correction on top of that (found by testing
// against TheSportsDB directly): abbreviated forms that collide with a reserve
// team or an unrelated club/sport of the same short name. Nottingham Forest has
// no findable Soccer entry under any name variant tried - it's left out on
// purpose rather than guessing a badge URL for it; it'll fall back to an
// initials badge instead, same as any other team not in this map.
const SEARCH_NAME_OVERRIDES = {
  "M'gladbach": "Borussia Monchengladbach",
  "Santander": "Racing de Santander",
  "Leeds": "Leeds United",
  "Ipswich": "Ipswich Town",
  "Lille": "Lille OSC",
  "AEK": "AEK Athens",
};

const UNDERSTAT_FULL_NAME = {
  // E0
  "Man City": "Manchester City",
  "Man United": "Manchester United",
  "Newcastle": "Newcastle United",
  "Nott'm Forest": "Nottingham Forest",
  "West Brom": "West Bromwich Albion",
  "Wolves": "Wolverhampton Wanderers",
  // SP1
  "Ath Bilbao": "Athletic Club",
  "Ath Madrid": "Atletico Madrid",
  "Betis": "Real Betis",
  "Celta": "Celta Vigo",
  "Espanol": "Espanyol",
  "Huesca": "SD Huesca",
  "La Coruna": "Deportivo La Coruna",
  "Oviedo": "Real Oviedo",
  "Sociedad": "Real Sociedad",
  "Valladolid": "Real Valladolid",
  "Vallecano": "Rayo Vallecano",
  // D1
  "Bielefeld": "Arminia Bielefeld",
  "Leverkusen": "Bayer Leverkusen",
  "Dortmund": "Borussia Dortmund",
  "Ein Frankfurt": "Eintracht Frankfurt",
  "FC Koln": "FC Cologne",
  "Heidenheim": "FC Heidenheim",
  "Fortuna Dusseldorf": "Fortuna Duesseldorf",
  "Greuther Furth": "Greuther Fuerth",
  "Hamburg": "Hamburger SV",
  "Hannover": "Hannover 96",
  "Hertha": "Hertha Berlin",
  "Mainz": "Mainz 05",
  "Nurnberg": "Nuernberg",
  "RB Leipzig": "RasenBallsport Leipzig",
  "St Pauli": "St. Pauli",
  "Stuttgart": "VfB Stuttgart",
  // I1
  "Milan": "AC Milan",
  "Parma": "Parma Calcio 1913",
  "Spal": "SPAL 2013",
  // F1
  "Clermont": "Clermont Foot",
  "Paris SG": "Paris Saint Germain",
  "St Etienne": "Saint-Etienne",
};

function searchNameFor(teamName) {
  return SEARCH_NAME_OVERRIDES[teamName] || UNDERSTAT_FULL_NAME[teamName] || teamName;
}

module.exports = { searchNameFor };
