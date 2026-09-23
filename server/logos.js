// Team crest lookup via TheSportsDB's free API, with a persistent local cache -
// each team is only ever looked up once. A genuinely missing team stays cached
// as `null` on purpose, so it isn't retried forever; the frontend falls back to
// a generated initials badge for any team with no logo.
//
// The free tier rate-limits fairly aggressively - a burst of requests starts
// returning an HTML error page instead of JSON partway through. That's a
// TRANSIENT failure and must never be cached as `null` (a rate limit is not
// the same fact as "this team has no crest") - it's retried with backoff
// instead, and simply left unresolved (not cached either way) if it still
// fails, so the next warmCache() run picks it up again.
//
// The search endpoint itself isn't perfectly reliable either: it can return a
// reserve team, a same-named club in another sport, or nothing at all for an
// abbreviated name. team_names.js maps known-tricky names to a search term
// that resolves correctly; results are additionally required to be strSport
// "Soccer" before being trusted at all, since a wrong logo is worse than none.

const fs = require("fs");
const path = require("path");
const { searchNameFor } = require("./team_names");

const CACHE_PATH = path.join(__dirname, "logo_cache.json");
const SEARCH_URL = "https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t=";
const REQUEST_DELAY_MS = 1200;

let cache = {};
try {
  cache = JSON.parse(fs.readFileSync(CACHE_PATH, "utf8"));
} catch {
  cache = {};
}

function saveCache() {
  fs.writeFileSync(CACHE_PATH, JSON.stringify(cache, null, 2));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Throws on any transient failure (network error, rate limit, non-JSON body).
// Returns null only when the API gave a real answer with no Soccer match.
async function fetchLogoOnce(teamName) {
  const searchName = searchNameFor(teamName);
  const res = await fetch(SEARCH_URL + encodeURIComponent(searchName));
  const text = await res.text();
  if (!res.ok || text.trimStart().startsWith("<")) {
    throw new Error(`HTTP ${res.status} (likely rate-limited)`);
  }
  const data = JSON.parse(text);
  const match = (data.teams || []).find((t) => t.strSport === "Soccer");
  return match?.strBadge || null;
}

async function fetchLogoWithRetry(teamName, retries = 3) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      return await fetchLogoOnce(teamName);
    } catch (err) {
      if (attempt === retries) throw err;
      await sleep(REQUEST_DELAY_MS * attempt * 2); // back off and give the free API a break
    }
  }
}

// Returns the cached logo URL (or null), or undefined if never looked up - use
// this in request handlers, so a page load is never blocked on an external API.
function getCachedLogo(teamName) {
  return Object.prototype.hasOwnProperty.call(cache, teamName) ? cache[teamName] : undefined;
}

// Looks up and caches any team names not already known. Run this from a
// one-off script (warm_logos.js), not from a request handler - it's slow on
// a cold cache (one network call per unknown team, with a polite delay).
async function warmCache(teamNames) {
  const unknown = [...new Set(teamNames)].filter((name) => getCachedLogo(name) === undefined);
  console.log(`${unknown.length} team(s) not yet in the logo cache.`);
  let unresolved = 0;
  for (const name of unknown) {
    try {
      const url = await fetchLogoWithRetry(name);
      cache[name] = url;
      console.log(`  ${name}: ${url || "(not found - will use an initials badge)"}`);
    } catch (err) {
      unresolved++;
      console.log(`  ${name}: skipped for now (${err.message}) - will retry on the next warm-logos run`);
    }
    await sleep(REQUEST_DELAY_MS);
  }
  saveCache();
  if (unresolved > 0) {
    console.log(`\n${unresolved} team(s) still unresolved - run "npm run warm-logos" again to retry them.`);
  }
}

module.exports = { getCachedLogo, warmCache };
