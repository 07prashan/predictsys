// One-off: pre-fetches every team logo currently needed, so the live site never
// hits a cold cache. Run this once after adding new teams (e.g. after predict.py
// logs fixtures for a newly-promoted team) - `npm run warm-logos`.

const db = require("./db");
const { warmCache } = require("./logos");

async function main() {
  const rows = db.prepare("SELECT home_team AS team FROM predictions UNION SELECT away_team FROM predictions").all();
  const teamNames = rows.map((r) => r.team);
  await warmCache(teamNames);
  console.log("Done.");
}

main();
