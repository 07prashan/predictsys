// One-off: pre-fetches every club crest still missing, so the live site never hits a cold
// cache. Run this once after adding new teams (e.g. after predict.py logs fixtures for a
// newly-promoted team) - `npm run warm-logos`.
//
// Only teams predict.py stored no crest for are looked up: national teams, tennis players
// and ESPN-sourced club fixtures all arrive with an image URL already, so searching
// TheSportsDB for them would just spend its rate limit on names that don't need it.

const db = require("./db");
const { warmCache } = require("./logos");

async function main() {
  const rows = db
    .prepare(
      `SELECT home_team AS team FROM predictions WHERE home_logo IS NULL AND COALESCE(sport, 'football') = 'football' AND league NOT IN ('INT')
       UNION
       SELECT away_team FROM predictions WHERE away_logo IS NULL AND COALESCE(sport, 'football') = 'football' AND league NOT IN ('INT')`
    )
    .all();
  const teamNames = rows.map((r) => r.team);
  await warmCache(teamNames);
  console.log("Done.");
}

main();
