// One place that answers "what's the crest/flag for this name?".
//
// predict.py stores the image URL the data feed supplied (national flags, player flags,
// club crests) on each prediction, so those are used first - they cover every name the
// site shows. The older TheSportsDB cache (logos.js) is the fallback for club-league rows
// predicted before that existed. Returns null when neither knows the name, and the
// frontend then draws an initials badge.

const db = require("./db");
const { getCachedLogo } = require("./logos");

const fromPredictions = db.prepare(
  `SELECT logo FROM (
     SELECT home_logo AS logo FROM predictions WHERE home_team = ? AND home_logo IS NOT NULL
     UNION ALL
     SELECT away_logo FROM predictions WHERE away_team = ? AND away_logo IS NOT NULL
   ) LIMIT 1`
);

function logoFor(name) {
  return fromPredictions.get(name, name)?.logo || getCachedLogo(name) || null;
}

module.exports = { logoFor };
