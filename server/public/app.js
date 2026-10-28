const OUTCOME_LABEL = { H: "Home", D: "Draw", A: "Away" };
const matchDataByKey = new Map();

// How many days (today included) the day strip and "this week" scaffold cover. The pipeline
// predicts fourteen days ahead (predict.py's HORIZON_DAYS) - the two should move together.
const DAYS_AHEAD = 14;

// Minutes after kickoff at which a match counts as over and is dropped from the page for
// good. The server filters coarsely (a few hours) in UTC; this applies the real per-sport
// cutoff against the viewer's own clock, and keeps applying it while the page stays open.
const FINISHED_AFTER_MIN = { football: 150, tennis: 360, basketball: 180 };

const CATEGORIES = [
  { id: "all", label: "All sports" },
  { id: "leagues", label: "Football · Leagues" },
  { id: "intl", label: "Football · National teams" },
  { id: "nba", label: "Basketball · NBA" },
  { id: "atp", label: "Tennis · ATP" },
  { id: "wta", label: "Tennis · WTA" },
];
// ---- Data: static JSON files written by predict.py (src/site_export.py) ----

// Always revalidated: the files are replaced every few hours, and a stale copy would keep
// showing matches that are over.
async function getJson(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path} answered ${res.status}`);
  return res.json();
}

// Team and head-to-head data are split per league, so opening one profile downloads one small
// file instead of every team of every sport. Cleared whenever the main data is re-fetched.
const shardCache = new Map();
function getShard(kind, league) {
  const path = `data/${kind}/${encodeURIComponent(league)}.json`;
  if (!shardCache.has(path)) {
    shardCache.set(
      path,
      getJson(path).catch((err) => {
        shardCache.delete(path); // a failed load must be retryable
        throw err;
      })
    );
  }
  return shardCache.get(path);
}

// "Updated 12 min ago": if the scheduled refresh ever stalls, visitors can see that for themselves
// instead of trusting stale tips.
let generatedAt = null;
function renderUpdated() {
  const el = document.getElementById("updated-at");
  if (!el || generatedAt === null) return;
  const mins = Math.max(0, Math.round((Date.now() - generatedAt) / 60000));
  el.textContent = `Updated ${mins < 2 ? "just now" : mins < 90 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`}`;
  el.classList.toggle("stale", mins > 360);
}

function categoryOf(m) {
  if (m.sport === "tennis") return m.league === "WTA" ? "wta" : "atp";
  if (m.sport === "basketball") return "nba";
  return m.league === "INT" ? "intl" : "leagues";
}

// Everything interpolated into HTML below that came from data (team, player, tournament
// names) goes through this - those names now come from an outside feed, and an apostrophe
// or quote in one must not be able to break out of an attribute.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function pct(x) {
  return `${(x * 100).toFixed(0)}%`;
}

function initials(name) {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

// Renders a team's crest if we have one cached; otherwise an initials badge.
// If a cached URL ever goes stale, onerror swaps in the same initials badge
// rather than leaving a broken image icon.
function teamLogo(name, logoUrl) {
  const safeInitials = esc(initials(name));
  if (!logoUrl) return `<span class="team-logo team-logo-fallback">${safeInitials}</span>`;
  return `<img class="team-logo" src="${esc(logoUrl)}" alt="" loading="lazy"
    onerror="this.outerHTML='&lt;span class=&quot;team-logo team-logo-fallback&quot;&gt;${safeInitials}&lt;/span&gt;'">`;
}

// data-team + the clickable class are what let event delegation open a team
// profile instead of the match it's part of - see setUpDelegatedClicks().
// data-league says WHICH competition's profile (a name could exist in two).
// role="button" + tabindex make it keyboard-reachable too, not just mouse-clickable.
function teamWithLogo(name, logoUrl, league) {
  return `<span class="team clickable" data-team="${esc(name)}" data-league="${esc(league || "")}" role="button" tabindex="0" aria-label="View ${esc(name)} profile">${teamLogo(name, logoUrl)}<span class="team-name-text">${esc(name)}</span></span>`;
}

// The last five results as coloured dots, oldest to newest (so the right-most is the latest).
const FORM_WORD = { W: "win", D: "draw", L: "loss" };
function formDots(form, mini = false) {
  if (!form) return "";
  const dots = [...form].map((r) => `<i class="fd ${r}">${mini ? "" : r}</i>`).join("");
  const spoken = [...form].map((r) => FORM_WORD[r]).join(", ");
  return `<span class="form-dots${mini ? " mini" : ""}" role="img" aria-label="Recent form, oldest to newest: ${spoken}">${dots}</span>`;
}

function isTennis(m) {
  return m.sport === "tennis";
}

function isBasketball(m) {
  return m.sport === "basketball";
}

function outcomeText(m, code) {
  // tennis and basketball have no draw, so a side is named rather than "Home"/"Away"
  if (isTennis(m) || isBasketball(m)) return code === "H" ? m.home_team : m.away_team;
  return OUTCOME_LABEL[code];
}

function probBar(row) {
  if (isTennis(row) || isBasketball(row)) {
    return `
      <div class="prob-bar" title="${esc(row.home_team)} ${pct(row.prob_home)} / ${esc(row.away_team)} ${pct(row.prob_away)}">
        <span class="h" style="width:${row.prob_home * 100}%"></span>
        <span class="a" style="width:${row.prob_away * 100}%"></span>
      </div>
      <span class="prob-pct">${pct(row.prob_home)} / ${pct(row.prob_away)}</span>
    `;
  }
  return `
    <div class="prob-bar" title="Home ${pct(row.prob_home)} / Draw ${pct(row.prob_draw)} / Away ${pct(row.prob_away)}">
      <span class="h" style="width:${row.prob_home * 100}%"></span>
      <span class="d" style="width:${row.prob_draw * 100}%"></span>
      <span class="a" style="width:${row.prob_away * 100}%"></span>
    </div>
    <span class="prob-pct">${pct(row.prob_home)} / ${pct(row.prob_draw)} / ${pct(row.prob_away)}</span>
  `;
}

function formatDate(iso) {
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

// With the year - for head-to-head lists, which for national teams reach back decades.
function formatDateWithYear(iso) {
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatTime(ms) {
  return new Date(ms).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// "Sat, Oct 3 · 19:45" in the viewer's own timezone, or just the date for a legacy row with no kickoff time.
function formatKickoff(m) {
  if (m.start === null || m.start === undefined) return formatDate(m.match_date);
  return `${new Date(m.start).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} · ${formatTime(m.start)}`;
}

function todayLocal() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

// YYYY-MM-DD from LOCAL date fields - toISOString() converts to UTC first, which
// silently rolls the date back (or forward) a day for any timezone offset that
// crosses midnight, exactly the kind of off-by-one this must never have.
function localDateKey(d) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function daysBetween(dateStr, from) {
  const target = new Date(dateStr + "T00:00:00");
  return Math.round((target - from) / 86400000);
}

function dayLabel(dateStr, from) {
  const diff = daysBetween(dateStr, from);
  const full = new Date(dateStr + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });
  if (diff === 0) return `Today · ${full}`;
  if (diff === 1) return `Tomorrow · ${full}`;
  return full;
}

// Today + the following days as {date, label, weekday, dayMonth} - shared by the day strip,
// the stacked "all days" view and each competition's date dropdown in the competition view,
// so all three agree on exactly which days are on offer.
function buildWeekDates(from) {
  const days = [];
  for (let i = 0; i < DAYS_AHEAD; i++) {
    const d = new Date(from);
    d.setDate(d.getDate() + i);
    const key = localDateKey(d);
    days.push({
      date: key,
      label: dayLabel(key, from),
      weekday: i === 0 ? "Today" : d.toLocaleDateString(undefined, { weekday: "short" }),
      dayMonth: d.toLocaleDateString(undefined, { day: "numeric", month: "short" }),
    });
  }
  return days;
}

// ---- Which matches are still worth showing ----

function isFinished(m, now) {
  if (m.start !== null) return now > m.start + (FINISHED_AFTER_MIN[m.sport] ?? 150) * 60000;
  // an older row with no kickoff time: over once its whole date has passed
  return daysBetween(m.match_date, todayLocal()) < 0;
}

function prepare(rows) {
  const todayKey = localDateKey(todayLocal());
  return rows.map((m) => {
    const start = m.kickoff_utc ? Date.parse(m.kickoff_utc) : null;
    let localDate = start !== null ? localDateKey(new Date(start)) : m.match_date;
    // a match still in play that began before midnight belongs under today, not a day that's gone
    if (localDate < todayKey) localDate = todayKey;
    return { ...m, start, local_date: localDate, category: categoryOf(m) };
  });
}

let upcomingRows = [];
let upcomingView = "date";
// Competition codes in popularity order, as published with the predictions - the league a
// visitor came for is listed first, not wherever the alphabet happens to put it.
let leagueOrder = [];
const uiState = { category: "all", day: null }; // day null = "the first day that has matches"

// Upcoming matches only: anything already over is gone, as is anything past the window we promise.
function liveRows() {
  const now = Date.now();
  const lastDay = buildWeekDates(todayLocal()).at(-1).date;
  return upcomingRows.filter((m) => !isFinished(m, now) && m.local_date <= lastDay);
}

function rowsInCategory(rows = liveRows()) {
  return uiState.category === "all" ? rows : rows.filter((m) => m.category === uiState.category);
}

function byKickoff(a, b) {
  return (a.start ?? 0) - (b.start ?? 0) || a.home_team.localeCompare(b.home_team);
}

// ---- Tips: the one thing a visitor came for ----

// Confidence tiers drive the colour everywhere (row edge, tip label, meter), so a glance
// down a list separates strong tips from coin flips without reading a single number.
const TIERS = [
  { id: "strong", min: 0.75 },
  { id: "good", min: 0.65 },
  { id: "fair", min: 0.55 },
  { id: "low", min: 0 },
];
const tierOf = (p) => TIERS.find((t) => p >= t.min);

// Never print 100%: a model that rounds to certainty is overclaiming, and no tip is a sure thing.
const tipPct = (p) => `${Math.min(99, Math.round(p * 100))}%`;

// The "Top Tips" view lists only tips at least this confident.
const TOP_TIP_MIN = 0.65;

const TIP_WORDS = {
  "Home Win": "Home win",
  "Away Win": "Away win",
  "Both Teams to Score": "Both teams score",
  "Not Both Teams to Score": "Not both score",
  "Over 2.5 Goals": "Over 2.5 goals",
  "Under 2.5 Goals": "Under 2.5 goals",
};
// A tennis tip is always "<favourite> to win", and the favourite is picked out by name in the
// row, so the label itself can be short.
function tipText(m) {
  return isTennis(m) ? "To win" : TIP_WORDS[m.best_pick_label] || m.best_pick_label;
}

// Which side the tip backs ('H' / 'A'), so that team can be picked out by name; null when it
// backs neither (a draw, or a goals market).
function pickedSide(m) {
  if (isTennis(m)) return m.predicted_outcome;
  if (isBasketball(m)) return m.best_pick_market === "Moneyline" ? m.predicted_outcome : null;
  if (m.best_pick_market !== "Result") return null;
  return m.best_pick_label.startsWith("Home") ? "H" : m.best_pick_label.startsWith("Away") ? "A" : null;
}

const ROUND_SHORT = {
  "Round 1": "R1", "Round 2": "R2", "Round 3": "R3", "Round 4": "R4",
  "Round of 16": "R16", "Round of 32": "R32", "Round of 64": "R64", "Round of 128": "R128",
  Quarterfinal: "QF", Semifinal: "SF", Final: "F",
};

// ---- Match rows ----

function marketChip(label, value) {
  return `<div class="market-chip"><span class="market-label">${esc(label)}</span><span class="market-value">${value}</span></div>`;
}

function matchKey(m) {
  return `${m.league}|${m.match_date}|${m.home_team}|${m.away_team}`;
}

function setsOverText(m) {
  return m.sets_over_prob >= 0.5 ? `Over ${m.sets_line} (${pct(m.sets_over_prob)})` : `Under ${m.sets_line} (${pct(1 - m.sets_over_prob)})`;
}

function rowTeam(name, logo, side, picked, form) {
  const role = picked === null ? "" : picked === side ? " pick" : " other";
  return `<div class="row-team${role}">${teamLogo(name, logo)}<span class="name">${esc(name)}</span>${formDots(form, true)}</div>`;
}

// One match on one line: when, who, and - loudest of all - the tip with its confidence. Everything
// else (probabilities, markets, form, head-to-head) is one tap away in the detail view.
// opts.showDay: the date matters (a list spanning several days); opts.showComp: the row isn't
// already under a heading naming its competition.
function matchRow(m, opts = {}) {
  matchDataByKey.set(matchKey(m), m);
  const tier = tierOf(m.best_pick_prob);
  const picked = pickedSide(m);
  const round = isTennis(m) && m.round ? ROUND_SHORT[m.round] || m.round : "";
  const started = m.start !== null && Date.now() >= m.start;
  const weekday = m.start !== null ? new Date(m.start).toLocaleDateString(undefined, { weekday: "short" }) : "";
  const sub = started
    ? `<span class="live-badge" title="Kicked off - the result isn't in yet">In play</span>`
    : opts.showDay
      ? `<span class="row-sub">${esc(weekday)}</span>`
      : !opts.showComp && round
        ? `<span class="row-sub">${esc(round)}</span>`
        : "";
  const comp = opts.showComp ? `<span class="row-comp">${esc(m.competition)}${round ? ` · ${esc(round)}` : ""}</span>` : "";
  const label = `${m.home_team} vs ${m.away_team}. Tip: ${m.best_pick_label}, ${tipPct(m.best_pick_prob)} confidence`;
  return `
    <article class="match-row clickable tier-${tier.id}" data-match-key="${esc(matchKey(m))}" role="button" tabindex="0" aria-label="${esc(label)}">
      <div class="row-when"><span class="kickoff">${m.start !== null ? formatTime(m.start) : "-"}</span>${sub}</div>
      <div class="row-teams">
        ${comp}
        ${rowTeam(m.home_team, m.home_logo, "H", picked, m.home_form)}
        ${rowTeam(m.away_team, m.away_logo, "A", picked, m.away_form)}
      </div>
      <div class="row-tip" title="${esc(m.best_pick_label)}">
        <span class="tip-line"><span class="tip-label">${esc(tipText(m))}</span><span class="tip-pct">${tipPct(m.best_pick_prob)}</span></span>
        <span class="tip-meter" aria-hidden="true"><i style="width:${Math.round(m.best_pick_prob * 100)}%"></i></span>
      </div>
      <div class="row-extra">${isTennis(m) ? "Likely sets" : "Likely score"} <b>${m.correct_score_home}-${m.correct_score_away}</b></div>
    </article>
  `;
}

// ---- "By Date" view: day strip + the chosen day's matches, grouped by competition ----

function groupByCompetition(matches) {
  const groups = new Map();
  for (const m of matches) {
    const key = `${m.league}|${m.competition}`;
    if (!groups.has(key)) groups.set(key, { competition: m.competition, group: m.country, category: m.category, matches: [] });
    groups.get(key).matches.push(m);
  }
  // The card is read in stature order: the big leagues first in the published popularity
  // order, then the smaller leagues whose clubs are in Europe, then the rest - and inside a
  // smaller league, its European clubs' fixtures come before the rest of the card. Kickoff
  // breaks every tie, so within a league (and inside the big leagues, where every fixture
  // shares a rank) the day still reads chronologically.
  return [...groups.values()]
    .map((g) => ({ ...g, matches: g.matches.sort(byStature) }))
    .sort((a, b) => rankOf(a.matches[0]) - rankOf(b.matches[0]) || byKickoff(a.matches[0], b.matches[0]));
}

function competitionGroupHtml(g, opts) {
  const slam = g.matches[0].grand_slam ? `<span class="slam-badge">Grand Slam</span>` : "";
  return `
    <section class="comp-group">
      <div class="comp-header">
        <h3>${esc(g.competition)}${slam}</h3>
        <span class="comp-meta">${esc(g.group)} · ${g.matches.length} ${g.matches.length === 1 ? "match" : "matches"}</span>
      </div>
      <div class="match-list">${g.matches.map((m) => matchRow(m, opts)).join("")}</div>
    </section>
  `;
}

function emptyDayHtml(dayKey, rows, what = "matches scheduled for this selection") {
  const from = todayLocal();
  const next = buildWeekDates(from).find((d) => d.date > dayKey && rows.some((m) => m.local_date === d.date));
  const hint = next
    ? ` Next up: <button class="link-btn" data-goto-day="${next.date}">${esc(next.label)}</button>.`
    : "";
  return `<div class="empty-state small">No ${what} on ${esc(dayLabel(dayKey, from))}.${hint}</div>`;
}

function dayBodyHtml(dayKey, rows) {
  const matches = rows.filter((m) => m.local_date === dayKey);
  return matches.length ? groupByCompetition(matches).map((g) => competitionGroupHtml(g)).join("") : emptyDayHtml(dayKey, rows);
}

// null (auto) resolves to the first day with matches, so the page never opens on an empty
// day while the next one is full; "all" is the stacked every-day view.
function resolveDay(rows) {
  if (uiState.day) return uiState.day;
  const firstWithMatches = buildWeekDates(todayLocal()).find((d) => rows.some((m) => m.local_date === d.date));
  return firstWithMatches ? firstWithMatches.date : "all";
}

function renderDayStrip(rows, activeDay, [one, many] = ["match", "matches"]) {
  const strip = document.getElementById("day-strip");
  const counts = new Map();
  for (const m of rows) counts.set(m.local_date, (counts.get(m.local_date) || 0) + 1);

  const pill = (key, top, middle, count) => `
    <button class="day-pill ${key === activeDay ? "active" : ""} ${count ? "" : "empty"}" data-day="${key}" aria-pressed="${key === activeDay}">
      <span class="day-name">${esc(top)}</span>
      <span class="day-num">${esc(middle)}</span>
      <span class="day-count">${count} ${count === 1 ? one : many}</span>
    </button>`;

  strip.innerHTML =
    pill("all", "All", "days", rows.length) +
    buildWeekDates(todayLocal())
      .map((d) => pill(d.date, d.weekday, d.dayMonth, counts.get(d.date) || 0))
      .join("");
  strip.hidden = upcomingView === "country";
}

function renderByDate(el, rows) {
  const active = resolveDay(rows);
  renderDayStrip(rows, active);

  if (active !== "all") {
    el.innerHTML = `<h2 class="day-heading">${esc(dayLabel(active, todayLocal()))}</h2>${dayBodyHtml(active, rows)}`;
    return;
  }
  // every day stacked, empty ones included - a day is never silently missing
  el.innerHTML = buildWeekDates(todayLocal())
    .map((d) => {
      const n = rows.filter((m) => m.local_date === d.date).length;
      return `<div class="league-group" data-date="${d.date}"><h2>${esc(d.label)} <span class="day-total">${n} ${n === 1 ? "match" : "matches"}</span></h2>${dayBodyHtml(d.date, rows)}</div>`;
    })
    .join("");
}

// ---- "Top Tips" view: the strongest tips first, across every competition ----

function renderTopTips(el, rows) {
  const tips = rows.filter((m) => m.best_pick_prob >= TOP_TIP_MIN);
  const active = resolveDay(tips);
  renderDayStrip(tips, active, ["tip", "tips"]);

  const label = active === "all" ? `the next ${DAYS_AHEAD} days` : dayLabel(active, todayLocal());
  const shown = (active === "all" ? tips : tips.filter((m) => m.local_date === active)).sort(
    (a, b) => b.best_pick_prob - a.best_pick_prob || byKickoff(a, b)
  );
  const minPct = Math.round(TOP_TIP_MIN * 100);
  el.innerHTML = `
    <h2 class="day-heading">Top tips <span class="day-total">${esc(label)}</span></h2>
    <p class="view-note">Tips we're ${minPct}%+ confident in, strongest first.</p>
    ${
      shown.length
        ? `<div class="match-list">${shown.map((m) => matchRow(m, { showComp: true, showDay: active === "all" })).join("")}</div>`
        : emptyDayHtml(active === "all" ? localDateKey(todayLocal()) : active, tips, `tips of ${minPct}%+ for this selection`)
    }
  `;
}

// ---- "By Competition" view: country / tour -> competition, each with its own date dropdown ----

// Position of a competition in the published popularity order (unknown codes go last).
function leagueRank(code) {
  const i = leagueOrder.indexOf(code);
  return i === -1 ? leagueOrder.length : i;
}

// One fixture's place in the order the page shows: the popularity of its competition, except
// that in a smaller league the clubs contesting Europe come ahead of their domestic peers -
// Slavia before Slovan, Copenhagen before the rest of the Danish card. The big leagues keep
// their pure popularity order because nothing outside that block can reach their ranks.
// A snapshot from before this field existed falls back to plain popularity.
const rankOf = (m) => (typeof m.fixture_rank === "number" ? m.fixture_rank : leagueRank(m.league));
const byStature = (a, b) => rankOf(a) - rankOf(b) || byKickoff(a, b);

function groupByCountry(rows) {
  const byCountry = new Map();
  for (const row of rows) {
    if (!byCountry.has(row.country)) byCountry.set(row.country, new Map());
    const byComp = byCountry.get(row.country);
    const key = `${row.league}|${row.competition}`;
    if (!byComp.has(key)) byComp.set(key, { key, competition: row.competition, leagueCode: row.league, matches: [] });
    byComp.get(key).matches.push(row);
  }
  // Competitions follow the popularity order (Premier League, Champions League, La Liga, ...),
  // and a country sits where its best-ranked competition does - so England and Spain lead the
  // page instead of sorting into the alphabet.
  return [...byCountry.entries()]
    .map(([country, byComp]) => {
      const competitions = [...byComp.values()]
        .map((c) => ({ ...c, matches: c.matches.sort(byStature) }))
        .sort((a, b) => rankOf(a.matches[0]) - rankOf(b.matches[0]) || a.competition.localeCompare(b.competition));
      return { country, competitions, rank: Math.min(...competitions.map((c) => rankOf(c.matches[0]))) };
    })
    .sort((a, b) => a.rank - b.rank || a.country.localeCompare(b.country));
}

function matchesForCompetitionOnDate(key, date) {
  return rowsInCategory()
    .filter((m) => `${m.league}|${m.competition}` === key && m.local_date === date)
    .sort(byKickoff);
}

function competitionMatchesHtml(matches) {
  return matches.length
    ? matches.map((m) => matchRow(m)).join("")
    : `<div class="empty-state small">No fixtures scheduled for this day.</div>`;
}

// Each competition gets its own date dropdown (defaulting to its first day with matches)
// rather than showing every upcoming day at once - the dropdown only ever re-renders this
// one competition's match-list (see setUpCountryDateSelects), never refetching.
function competitionBlockHtml({ key, competition, matches }, weekDates) {
  const counts = new Map();
  for (const m of matches) counts.set(m.local_date, (counts.get(m.local_date) || 0) + 1);
  const defaultDate = (weekDates.find((d) => counts.has(d.date)) || weekDates[0]).date;
  const options = weekDates
    .map((d) => `<option value="${d.date}" ${d.date === defaultDate ? "selected" : ""}>${esc(d.label)} (${counts.get(d.date) || 0})</option>`)
    .join("");
  return `
    <div class="competition-block">
      <div class="competition-header">
        <h3>${esc(competition)}</h3>
        <select class="date-select" data-comp="${esc(key)}" aria-label="Date for ${esc(competition)}">${options}</select>
      </div>
      <div class="match-list" data-comp-matches="${esc(key)}">
        ${competitionMatchesHtml(matchesForCompetitionOnDate(key, defaultDate))}
      </div>
    </div>
  `;
}

function countryGroupHtml({ country, competitions }, weekDates) {
  return `
    <div class="league-group" data-country="${esc(country)}">
      <h2>${esc(country)}</h2>
      ${competitions.map((c) => competitionBlockHtml(c, weekDates)).join("")}
    </div>
  `;
}

function renderByCountry(el, rows) {
  document.getElementById("day-strip").hidden = true;
  const weekDates = buildWeekDates(todayLocal());
  const countries = groupByCountry(rows);
  el.innerHTML = countries.length
    ? countries.map((c) => countryGroupHtml(c, weekDates)).join("")
    : `<div class="empty-state">No upcoming matches scheduled for this selection.</div>`;
}

// Delegated once on the container, since #upcoming-content's innerHTML (and every
// .date-select inside it) is replaced wholesale on each render.
function setUpCountryDateSelects() {
  document.getElementById("upcoming-content").addEventListener("change", (e) => {
    const select = e.target.closest(".date-select");
    if (!select) return;
    const grid = [...document.querySelectorAll("[data-comp-matches]")].find((g) => g.dataset.compMatches === select.dataset.comp);
    if (!grid) return;
    grid.innerHTML = competitionMatchesHtml(matchesForCompetitionOnDate(select.dataset.comp, select.value));
  });
}

// ---- Rendering + filters ----

function renderCategoryChips() {
  const live = liveRows();
  const counts = { all: live.length };
  for (const m of live) counts[m.category] = (counts[m.category] || 0) + 1;
  document.getElementById("category-chips").innerHTML = CATEGORIES.map(
    (c) => `
      <button class="chip ${c.id === uiState.category ? "active" : ""}" data-category="${c.id}" aria-pressed="${c.id === uiState.category}">
        ${esc(c.label)} <span class="chip-count">${counts[c.id] || 0}</span>
      </button>`
  ).join("");
}

let lastRenderKey = "";

// Re-renders whatever's already been fetched under the current filters and view - switching
// any of them is instant and never re-hits the API. `force` skips the "nothing changed" check.
function renderUpcoming(force = true) {
  const el = document.getElementById("upcoming-content");
  const rows = rowsInCategory();

  // the minute timer calls this too, to drop matches that have just finished - but only
  // actually repaints when what's on screen would differ, so reading isn't interrupted
  const key = `${upcomingView}|${uiState.category}|${uiState.day}|${rows.map((m) => matchKey(m)).join(",")}`;
  if (!force && key === lastRenderKey) return;
  lastRenderKey = key;

  renderCategoryChips();
  if (upcomingView === "country") renderByCountry(el, rows);
  else if (upcomingView === "tips") renderTopTips(el, rows);
  else renderByDate(el, rows);
}

async function loadUpcoming() {
  const el = document.getElementById("upcoming-content");
  try {
    const data = await getJson("data/predictions.json");
    shardCache.clear();
    generatedAt = Date.parse(data.generated_at);
    leagueOrder = Array.isArray(data.league_order) ? data.league_order : [];
    upcomingRows = prepare(data.rows);
    renderUpdated();
    renderUpcoming(false);
  } catch (err) {
    // keep whatever's already on screen if this was just a background refresh
    if (!upcomingRows.length) el.innerHTML = `<div class="empty-state">Couldn't load predictions: ${esc(err.message)}</div>`;
  }
}

function setUpFilters() {
  document.getElementById("category-chips").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-category]");
    if (!chip || chip.dataset.category === uiState.category) return;
    uiState.category = chip.dataset.category;
    renderUpcoming();
  });
  document.getElementById("day-strip").addEventListener("click", (e) => {
    const pill = e.target.closest("[data-day]");
    if (!pill) return;
    uiState.day = pill.dataset.day;
    renderUpcoming();
  });
  // "Next up: Sat, Oct 10" in an empty-day message
  document.getElementById("upcoming-content").addEventListener("click", (e) => {
    const goto = e.target.closest("[data-goto-day]");
    if (!goto) return;
    uiState.day = goto.dataset.gotoDay;
    renderUpcoming();
  });
}

function setUpViewToggle() {
  const buttons = [...document.querySelectorAll(".view-toggle-btn")];
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.view === upcomingView) return;
      buttons.forEach((b) => {
        const isActive = b === btn;
        b.classList.toggle("active", isActive);
        b.setAttribute("aria-pressed", String(isActive));
      });
      upcomingView = btn.dataset.view;
      renderUpcoming();
    });
  });
}

// A page left open stays honest: finished matches drop off by themselves (checked each
// minute), and the data is re-fetched every few minutes to pick up new fixtures/results.
function setUpAutoRefresh() {
  setInterval(() => {
    if (document.hidden) return;
    renderUpcoming(false);
    renderUpdated();
  }, 60 * 1000);
  setInterval(() => {
    if (!document.hidden) {
      loadUpcoming();
      loadSlips();
    }
  }, 5 * 60 * 1000);
}

// ---- Track record ----

async function loadScoreboard() {
  const el = document.getElementById("scoreboard-content");
  try {
    const rows = await getJson("data/scoreboard.json");
    if (rows.length === 0) {
      el.innerHTML = `<div class="empty-state">No predictions have been settled against a real result yet - the track record fills in once fixtures are played and predict.py's next run grades them.</div>`;
      return;
    }
    const all = rows.find((r) => r.league === "ALL");
    const perLeague = rows.filter((r) => r.league !== "ALL");
    const card = (r, isAll) => `
      <div class="score-card ${isAll ? "all" : ""}">
        <h3>${esc(r.league_name)}</h3>
        <div class="accuracy">${pct(r.accuracy)}</div>
        <div class="detail">${r.n_settled} predictions graded &middot; log-loss ${r.log_loss.toFixed(3)}</div>
      </div>
    `;
    el.innerHTML = `
      <h2 style="margin-top:0">Track record</h2>
      <div class="scoreboard-grid">
        ${card(all, true)}
        ${perLeague.map((r) => card(r, false)).join("")}
      </div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="empty-state">Couldn't load the scoreboard: ${esc(err.message)}</div>`;
  }
}

function historyLeagueCell(r) {
  return isTennis(r) || isBasketball(r) || r.league === "INT" ? r.competition : r.league;
}

async function loadHistory() {
  const el = document.getElementById("history-content");
  try {
    const rows = prepare(await getJson("data/history.json"));
    if (rows.length === 0) return;
    el.innerHTML = `
      <h2>Recent graded predictions</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>Date</th><th>Competition</th><th>Match</th><th>Predicted</th><th>Actual</th><th>Score</th><th>Result</th></tr>
          </thead>
          <tbody>
            ${rows
              .map((r) => {
                matchDataByKey.set(matchKey(r), r);
                return `
              <tr class="clickable" data-match-key="${esc(matchKey(r))}" tabindex="0" aria-label="View match details: ${esc(r.home_team)} vs ${esc(r.away_team)}">
                <td>${formatDate(r.start !== null ? localDateKey(new Date(r.start)) : r.match_date)}</td>
                <td>${esc(historyLeagueCell(r))}</td>
                <td class="matchup">${teamWithLogo(r.home_team, r.home_logo, r.league)}<span class="vs">vs</span>${teamWithLogo(r.away_team, r.away_logo, r.league)}</td>
                <td><span class="outcome-tag ${r.predicted_outcome}">${esc(outcomeText(r, r.predicted_outcome))}</span></td>
                <td><span class="outcome-tag ${r.actual_outcome}">${esc(outcomeText(r, r.actual_outcome))}</span></td>
                <td class="score-cell">${r.actual_home_goals}-${r.actual_away_goals} <span class="predicted-score">(picked ${r.correct_score_home}-${r.correct_score_away})</span></td>
                <td class="result-mark ${r.correct ? "correct" : "incorrect"}">${r.correct ? "Correct" : "Wrong"}</td>
              </tr>
            `;
              })
              .join("")}
          </tbody>
        </table>
      </div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="empty-state">Couldn't load prediction history: ${esc(err.message)}</div>`;
  }
}

// ---- Modal: match detail ----

function formLine(matches, league) {
  if (!matches.length) return `<p class="empty-state">No recent matches on record.</p>`;
  return `<ul class="form-list">${matches
    .map(
      (m) => `
    <li>
      <span class="form-badge ${m.outcome}">${m.outcome}</span>
      <span class="form-date">${formatDate(m.date)}</span>
      <span class="form-opponent">${m.venue === "H" ? "vs" : "@"} ${teamWithLogo(m.opponent, m.opponent_logo, league)}</span>
      <span class="form-score">${m.goals_for}-${m.goals_against}</span>
    </li>
  `
    )
    .join("")}</ul>`;
}

function footballMarkets(m) {
  return `
    ${marketChip("Correct Score", `${m.correct_score_home}-${m.correct_score_away}`)}
    ${marketChip("BTTS", m.btts_yes_prob >= 0.5 ? `Yes ${pct(m.btts_yes_prob)}` : `No ${pct(1 - m.btts_yes_prob)}`)}
    ${marketChip("Over/Under 1.5", m.over_1_5_prob >= 0.5 ? `Over (${pct(m.over_1_5_prob)})` : `Under (${pct(1 - m.over_1_5_prob)})`)}
    ${marketChip("Over/Under 2.5", m.over_2_5_prob >= 0.5 ? `Over (${pct(m.over_2_5_prob)})` : `Under (${pct(1 - m.over_2_5_prob)})`)}
    ${marketChip("Over/Under 3.5", m.over_3_5_prob >= 0.5 ? `Over (${pct(m.over_3_5_prob)})` : `Under (${pct(1 - m.over_3_5_prob)})`)}
    ${marketChip("Double Chance 1X", pct(m.double_chance_1x_prob))}
    ${marketChip("Double Chance X2", pct(m.double_chance_x2_prob))}
    ${marketChip("Double Chance 12", pct(m.double_chance_12_prob))}
    ${marketChip("Home Clean Sheet", pct(m.home_clean_sheet_prob))}
    ${marketChip("Away Clean Sheet", pct(m.away_clean_sheet_prob))}
  `;
}

function tennisMarkets(m) {
  return `
    ${marketChip(`${m.home_team} to win`, pct(m.prob_home))}
    ${marketChip(`${m.away_team} to win`, pct(m.prob_away))}
    ${marketChip("Predicted Sets", `${m.correct_score_home}-${m.correct_score_away}`)}
    ${marketChip("Straight Sets", `${pct(m.straight_sets_prob)}`)}
    ${marketChip("Goes the Distance", pct(m.goes_the_distance_prob))}
    ${marketChip(`Sets Over/Under ${m.sets_line}`, setsOverText(m))}
    ${marketChip("Surface", esc(m.surface || "-"))}
    ${marketChip("Format", `Best of ${m.best_of || 3}`)}
  `;
}

function basketballMarkets(m) {
  const line = m.total_line;
  const over = m.over_prob;
  const margin = m.predicted_spread;
  return `
    ${marketChip(`${m.home_team} to win`, pct(m.prob_home))}
    ${marketChip(`${m.away_team} to win`, pct(m.prob_away))}
    ${marketChip("Predicted Score", `${m.correct_score_home}-${m.correct_score_away}`)}
    ${line != null ? marketChip(`Total Over/Under ${line}`, over >= 0.5 ? `Over (${pct(over)})` : `Under (${pct(1 - over)})`) : ""}
    ${margin != null ? marketChip("Predicted Margin", `${margin > 0 ? m.home_team : m.away_team} by ${Math.abs(margin)}`) : ""}
    ${m.predicted_total != null ? marketChip("Predicted Total", m.predicted_total) : ""}
    ${m.home_rating != null ? marketChip("Elo Rating", `${m.home_rating} / ${m.away_rating}`) : ""}
  `;
}

function formComparison(m) {
  if (!m.home_form && !m.away_form) return "";
  const rating = (r) => (r ? ` <span class="rating">${r}</span>` : "");
  return `
    <p class="section-title">Form &amp; rating</p>
    <div class="form-compare">
      <div class="form-compare-side">
        <span class="form-compare-name">${esc(m.home_team)}</span>
        ${formDots(m.home_form)}${rating(m.home_rating)}
      </div>
      <div class="form-compare-side away">
        <span class="form-compare-name">${esc(m.away_team)}</span>
        ${formDots(m.away_form)}${rating(m.away_rating)}
      </div>
    </div>
    ${m.limited_data ? `<p class="data-note">One of these players has little tour-level history in our data, so treat this prediction with extra caution.</p>` : ""}
  `;
}

function tipBanner(m) {
  const tier = tierOf(m.best_pick_prob);
  return `
    <div class="modal-tip tier-${tier.id}">
      <span class="modal-tip-kicker">Our tip</span>
      <span class="tip-label">${esc(m.best_pick_label)}</span>
      <span class="tip-pct">${tipPct(m.best_pick_prob)}</span>
    </div>`;
}

async function renderMatchDetail(m) {
  const content = document.getElementById("modal-content");
  const tennis = isTennis(m);
  const sub = [m.competition, tennis ? m.round : null, formatKickoff(m)].filter(Boolean).map(esc).join(" &middot; ");
  const final =
    m.actual_outcome != null
      ? `<p class="final-score">Final: <strong>${m.actual_home_goals}-${m.actual_away_goals}</strong></p>`
      : "";
  content.innerHTML = `
    <div class="modal-header-teams">
      ${teamWithLogo(m.home_team, m.home_logo, m.league)}
      <span class="vs">vs</span>
      ${teamWithLogo(m.away_team, m.away_logo, m.league)}
    </div>
    <div class="modal-subtitle">${sub}</div>
    ${final}
    ${tipBanner(m)}

    <div class="match-result-row" style="justify-content:center">
      ${probBar(m)}
      <span class="outcome-tag ${m.predicted_outcome}">${esc(outcomeText(m, m.predicted_outcome))}</span>
    </div>

    ${formComparison(m)}

    <p class="section-title">All markets</p>
    <div class="detail-market-grid">${tennis ? tennisMarkets(m) : isBasketball(m) ? basketballMarkets(m) : footballMarkets(m)}</div>

    <p class="section-title">Head-to-head</p>
    <div id="h2h-slot" class="loading">Loading head-to-head history...</div>
  `;
  showModal();

  try {
    const pairs = await getShard("h2h", m.league);
    const h2h = pairs[`${m.home_team}|${m.away_team}`] || { total: 0, summary: { home_wins: 0, draws: 0, away_wins: 0 }, meetings: [] };
    const slot = document.getElementById("h2h-slot");
    if (h2h.total === 0) {
      slot.innerHTML = `<p class="empty-state">These two haven't met in our records.</p>`;
      return;
    }
    slot.innerHTML = `
      <div class="h2h-summary">
        <div class="stat"><div class="num">${h2h.summary.home_wins}</div><div class="lbl">${esc(m.home_team)} wins</div></div>
        ${tennis ? "" : `<div class="stat"><div class="num">${h2h.summary.draws}</div><div class="lbl">Draws</div></div>`}
        <div class="stat"><div class="num">${h2h.summary.away_wins}</div><div class="lbl">${esc(m.away_team)} wins</div></div>
      </div>
      <ul class="h2h-list">
        ${h2h.meetings
          .slice(0, 10)
          .map(
            (g) => `
          <li>
            <span class="h2h-date">${formatDateWithYear(g.date)}</span>
            <span>${esc(g.home_team)} ${tennis ? "def." : "vs"} ${esc(g.away_team)}</span>
            <span class="h2h-score">${g.home_goals}-${g.away_goals}</span>
          </li>
        `
          )
          .join("")}
      </ul>
    `;
  } catch (err) {
    document.getElementById("h2h-slot").innerHTML = `<p class="empty-state">Couldn't load head-to-head: ${esc(err.message)}</p>`;
  }
}

// ---- Modal: team / player profile ----

async function renderTeamProfile(teamName, league) {
  const content = document.getElementById("modal-content");
  content.innerHTML = `<div class="loading">Loading ${esc(teamName)}...</div>`;
  showModal();

  try {
    const t = league ? (await getShard("teams", league))[teamName] : null;
    if (!t) {
      content.innerHTML = `<p class="empty-state">No profile for ${esc(teamName)} yet.</p>`;
      return;
    }
    const tennis = t.sport === "tennis";
    const rankLabel = tennis ? "of active players by rating" : "of " + t.league_size + " by rating";
    const lastStat = tennis || t.sport === "basketball"
      ? `<div class="stat"><div class="num">${t.recent_win_rate !== null ? pct(t.recent_win_rate) : "-"}</div><div class="lbl">Wins (last 10)</div></div>`
      : `<div class="stat"><div class="num">${t.recent_form_ppg !== null ? t.recent_form_ppg.toFixed(2) : "-"}</div><div class="lbl">Pts/game (last 10)</div></div>`;
    content.innerHTML = `
      <div class="modal-header-teams">
        ${teamLogo(t.team, t.logo)}
      </div>
      <div class="modal-subtitle" style="font-size:1.1rem;font-weight:700;color:var(--text)">${esc(t.team)}</div>
      <div class="modal-subtitle">${esc(t.league_name)}</div>

      <div class="profile-stats">
        <div class="stat"><div class="num">#${t.league_rank}</div><div class="lbl">${esc(rankLabel)}</div></div>
        <div class="stat"><div class="num">${Math.round(t.elo_rating)}</div><div class="lbl">Elo rating</div></div>
        ${lastStat}
      </div>

      <p class="section-title">Recent form</p>
      ${formLine(t.recent_matches, t.league)}

      ${
        t.next_fixture
          ? `<p class="next-fixture-note">Next: ${esc(t.next_fixture.home_team)} vs ${esc(t.next_fixture.away_team)} on ${esc(
              t.next_fixture.kickoff_utc ? formatKickoff({ start: Date.parse(t.next_fixture.kickoff_utc), match_date: t.next_fixture.match_date }) : formatDate(t.next_fixture.match_date)
            )}</p>`
          : ""
      }
    `;
  } catch (err) {
    content.innerHTML = `<p class="empty-state">Couldn't load ${esc(teamName)}: ${esc(err.message)}</p>`;
  }
}

// ---- Modal plumbing ----

let lastFocusedEl = null;

function showModal() {
  lastFocusedEl = document.activeElement;
  document.getElementById("modal-overlay").classList.remove("hidden");
  document.getElementById("modal-close").focus();
}
function hideModal() {
  const overlay = document.getElementById("modal-overlay");
  if (overlay.classList.contains("hidden")) return;
  overlay.classList.add("hidden");
  if (lastFocusedEl && document.contains(lastFocusedEl)) lastFocusedEl.focus();
}

// Keeps Tab from leaving the modal while it's open, since everything behind
// it is inert as far as the user should be concerned.
function trapFocus(e) {
  const overlay = document.getElementById("modal-overlay");
  if (overlay.classList.contains("hidden") || e.key !== "Tab") return;
  const focusables = overlay.querySelectorAll(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function setUpModal() {
  document.getElementById("modal-close").addEventListener("click", hideModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") hideModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideModal();
    else trapFocus(e);
  });
}

// Shared by both the click and keydown delegates below: a team hit opens that
// team's profile, anything else with a match key opens the match detail.
function activateAt(target) {
  const teamEl = target.closest(".team[data-team]");
  if (teamEl) return renderTeamProfile(teamEl.dataset.team, teamEl.dataset.league);
  const matchEl = target.closest("[data-match-key]");
  if (matchEl) {
    const data = matchDataByKey.get(matchEl.dataset.matchKey);
    if (data) renderMatchDetail(data);
  }
}

// A click on a team name opens that team's profile; a click anywhere else on
// a match card/row opens the match detail - team clicks are checked first and
// stop propagating so they don't also trigger the match they're inside of.
// Enter/Space do the same for keyboard users tabbed onto a card/row/team,
// since those are plain elements made focusable via role="button"/tabindex.
function setUpDelegatedClicks() {
  document.addEventListener("click", (e) => {
    const teamEl = e.target.closest(".team[data-team]");
    if (teamEl) {
      e.stopPropagation();
      renderTeamProfile(teamEl.dataset.team, teamEl.dataset.league);
      return;
    }
    const matchEl = e.target.closest("[data-match-key]");
    if (matchEl) {
      const data = matchDataByKey.get(matchEl.dataset.matchKey);
      if (data) renderMatchDetail(data);
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (!e.target.closest(".clickable")) return;
    e.preventDefault();
    activateAt(e.target);
  });
}

function activateTab(btn, buttons) {
  buttons.forEach((b) => {
    const isActive = b === btn;
    b.classList.toggle("active", isActive);
    b.setAttribute("aria-selected", String(isActive));
    b.tabIndex = isActive ? 0 : -1;
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.getElementById(btn.dataset.tab).classList.add("active");
}

function setUpTabs() {
  const buttons = [...document.querySelectorAll(".tab-btn")];
  buttons.forEach((btn, i) => {
    btn.addEventListener("click", () => activateTab(btn, buttons));
    // Arrow-key roving focus, per the standard tablist keyboard pattern.
    btn.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = buttons[(i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      activateTab(next, buttons);
    });
  });
}

// Adds a shadow once the page scrolls under the sticky header, so it reads
// as elevated above the content instead of blending flat into it.
function setUpHeaderScroll() {
  const header = document.getElementById("site-header");
  const onScroll = () => header.classList.toggle("scrolled", window.scrollY > 4);
  document.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
}

// ---- Filter slips: low-odds accumulators for today and the next few days ----
//
// slips.json (src/slips.py) hands us, per window, a handful of accumulators built from the
// day's most confident selections priced 1.10-1.40, each combining to 2.00-4.50. Nothing
// here recomputes anything - it just presents what the pipeline already decided.

let slipsData = null;
let slipFilter = null;

// Slip windows are 4am -> 4am Kathmandu time (see src/slips.py), so everything on a slip is
// rendered in Asia/Kathmandu no matter what timezone the viewer's browser is set to - otherwise
// the window boundary would read as 9:45pm (or worse, a different date) outside Nepal.
const SLIP_TZ = "Asia/Kathmandu";

function formatSlipKickoff(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  const date = new Date(ms).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: SLIP_TZ });
  const time = new Date(ms).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: SLIP_TZ });
  return `${date} \u00b7 ${time}`;
}

// "Oct 9, 4:00 AM" - the day-cycle boundary itself, in Kathmandu time.
function formatSlipDate(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  return new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: SLIP_TZ });
}

// A slip leg stores no match key of its own, so look its fixture up by league + teams to make
// the leg open the same detail view every other row does.
function findMatch(league, home, away) {
  return upcomingRows.find((m) => m.league === league && m.home_team === home && m.away_team === away);
}

function slipLegHtml(leg) {
  const match = findMatch(leg.league, leg.home_team, leg.away_team);
  const key = match ? matchKey(match) : "";
  if (match) matchDataByKey.set(key, match);
  const live = leg.odds_source === "1xlite";
  const label = match ? `View ${leg.home_team} vs ${leg.away_team}` : `${leg.home_team} vs ${leg.away_team}`;
  return `
    <li class="slip-leg${match ? " clickable" : ""}" ${match ? `data-match-key="${esc(key)}" role="button" tabindex="0" aria-label="${esc(label)}"` : ""}>
      <span class="slip-leg-when">${esc(formatSlipKickoff(leg.kickoff_utc))}</span>
      <span class="slip-leg-match">${esc(leg.home_team)}<span class="vs">vs</span>${esc(leg.away_team)}</span>
      <span class="slip-leg-pick">${esc(leg.label)}</span>
      <span class="slip-leg-odds ${live ? "live" : "model"}" title="${live ? "Price from the 1xLite betting app" : "Model's fair price (1 / probability)"}">${leg.odds.toFixed(2)}</span>
      ${leg.insight ? `<span class="slip-leg-why">${esc(leg.insight)}</span>` : ""}
      ${leg.note ? `<span class="slip-leg-note">Your view: ${esc(leg.note)}</span>` : ""}
    </li>`;
}

// One card renders every slip - the main bet, a combination, or a slip opened from a code -
// so the tab has a single list rather than a prediction view and a bet view side by side.
function slipCardHtml(slip, index, title, extraClass = "") {
  const legs = slip.legs.length;
  return `
    <article class="slip-card ${extraClass}${slip.edited ? " edited" : ""}">
      <div class="slip-head">
        <div class="slip-head-text">
          <span class="slip-name">${title}</span>
          <span class="slip-meta">${legs} ${legs === 1 ? "leg" : "legs"} &middot; win chance ${pct(slip.win_prob)}${slip.window ? ` &middot; ${esc(slip.window)}` : ""}${slip.edited ? ` &middot; <b>edited by you</b>` : ""}</span>
        </div>
        <div class="slip-total">
          <span class="slip-total-label">Total odds</span>
          <span class="slip-total-value">${slip.total_odds.toFixed(2)}</span>
        </div>
      </div>
      <ol class="slip-legs">${slip.legs.map(slipLegHtml).join("")}</ol>
      ${slipActionsHtml(slip, index)}
      ${slipEditorHtml(slip, index)}
    </article>`;
}

// ---- Slip codes: five characters to read out, plus a payload a saved slip keeps ----
//
// A slip's five-character code (K7M2Q) names it in the published feed, so anyone can open the
// same slip by typing it there. The longer PSB1- payload carries the slip itself - matches,
// picks, prices - and is what "Save slip" keeps on this device, so a saved slip still opens
// after the day's slips have been rebuilt underneath it. Neither is a 1xBet code: 1xBet only
// accepts codes its own app minted, which is why "Copy picks for 1xBet" hands over the
// selections as text instead of pretending a code would import there.

const BET_CODE_PREFIX = "PSB1-";
const CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
const CODE_LENGTH = 5;
const SAVED_SLIPS_KEY = "predictsys.saved_slips";
let renderedSlips = [];
let loadedSlip = null;

function decodeBetCode(text) {
  const code = (text || "").trim();
  if (!code.startsWith(BET_CODE_PREFIX)) throw new Error(`Not a PredictSys bet code - codes start with ${BET_CODE_PREFIX}`);
  const body = code.slice(BET_CODE_PREFIX.length);
  let payload;
  try {
    const b64 = body.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch (err) {
    throw new Error("That bet code is damaged - copy it again in full.");
  }
  if (!payload || payload.v !== 1 || !Array.isArray(payload.l)) {
    throw new Error("That bet code was made by a different version of the site.");
  }
  const legs = payload.l.map((p) => {
    const parts = String(p.m || "").split("|");
    return {
      match_key: p.m,
      league: parts[0] || "",
      home_team: parts[1] || "?",
      away_team: parts.slice(2).join("|") || "?",
      kickoff_utc: p.k,
      label: p.l || "",
      odds: Number(p.o) || 0,
      odds_source: p.x ? "1xlite" : "model",
    };
  });
  if (!legs.length) throw new Error("That bet code has no legs in it.");
  const product = legs.reduce((total, leg) => total * leg.odds, 1);
  return {
    window: payload.w || "",
    kind: payload.k === "main" ? "main" : "combo",
    total_odds: Number(payload.t) || product,
    win_prob: Number(payload.p) || 0,
    legs,
  };
}

// navigator.clipboard only exists in a secure context (https or localhost); the textarea
// fallback keeps copy working when the page is served over plain http elsewhere.
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      // clipboard permission denied - try the legacy path below
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  area.remove();
  return ok;
}

function flashButton(btn, label) {
  if (!btn.dataset.originalLabel) btn.dataset.originalLabel = btn.textContent;
  btn.textContent = label;
  btn.classList.add("done");
  clearTimeout(btn._flashTimer);
  btn._flashTimer = setTimeout(() => {
    btn.textContent = btn.dataset.originalLabel;
    btn.classList.remove("done");
  }, 1600);
}

// The picks as plain text, one line per leg - this is what actually gets a slip into a
// real betting app, since 1xBet will not import a code made anywhere but inside 1xBet.
function slipPlainText(slip) {
  const name = slip.kind === "main" ? "Main slip" : "Accumulator";
  const lines = slip.legs.map((leg, i) => {
    const when = formatSlipKickoff(leg.kickoff_utc);
    const line = `${i + 1}. ${leg.home_team} vs ${leg.away_team} - ${leg.label} @ ${leg.odds.toFixed(2)}${when ? ` (${when})` : ""}`;
    const why = leg.insight ? `\n   Why: ${leg.insight}` : "";
    const view = leg.note ? `\n   View: ${leg.note}` : "";
    return `${line}${why}${view}`;
  });
  return [
    `PredictSys slip - ${name}${slip.window ? ` (${slip.window})` : ""}${slip.code ? ` - slip code ${slip.code}` : ""}`,
    `${slip.legs.length} legs - total odds ${slip.total_odds.toFixed(2)} - win chance ${pct(slip.win_prob)}`,
    "Times are Kathmandu (UTC+5:45). Add every line below as one accumulator.",
    "",
    ...lines,
  ].join("\n");
}

function slipActionsHtml(slip, index) {
  const editable = Boolean(slip.code) && poolSize() > 0;
  const open = Boolean(slip.code) && openSlipEditors.has(slip.code);
  return `
      <div class="slip-actions">
        <span class="slip-code-now" title="Type this code in the box above to open this slip on any device">
          <span class="slip-code-label">Slip code</span>
          <code class="slip-code-value">${esc(slip.code || "\u00b7\u00b7\u00b7\u00b7\u00b7")}</code>
        </span>
        ${editable ? `<button class="slip-action${open ? " done" : ""}" data-edit-action="toggle" data-slip-idx="${index}" aria-expanded="${open}">${open ? "Close editor" : "Change slip"}</button>` : ""}
        <button class="slip-action" data-slip-action="copy-code" data-slip-idx="${index}" title="Copy this slip's code">Copy code</button>
        <button class="slip-action" data-slip-action="save" data-slip-idx="${index}" title="Keep this slip on this device">Save slip</button>
        <button class="slip-action" data-slip-action="copy-text" data-slip-idx="${index}" title="Copy the picks as text to add in the 1xBet bet slip">Copy picks for 1xBet</button>
        ${slip.isLoaded ? `<button class="slip-action" data-slip-action="close" aria-label="Close the loaded slip">&times;</button>` : ""}
      </div>`;
}

// ---- Changing a slip: swap fixtures, switch the market, keep your own view ----
//
// slips.json ships a `pool`: every fixture the window can draw from, with its whole board of
// markets and one reason-line per market shelf. The editor under a slip rewrites that slip
// from the pool - take a fixture off, add another, switch the bookmaker market, write your
// own view on the game - and the result is kept on this device (localStorage) under the
// slip's five-character code, so a rebuilt feed never wipes an edited slip. Only the totals
// are recomputed here: odds multiply, win chances multiply.

const SLIP_EDITS_KEY = "predictsys.slip_edits";
const openSlipEditors = new Set();

function poolIndex() {
  const map = new Map();
  for (const entry of (slipsData && slipsData.pool) || []) map.set(entry.match_key, entry);
  return map;
}

function poolSize() {
  return ((slipsData && slipsData.pool) || []).length;
}

function readSlipEdits() {
  try {
    const map = JSON.parse(localStorage.getItem(SLIP_EDITS_KEY) || "{}");
    return map && typeof map === "object" && !Array.isArray(map) ? map : {};
  } catch (err) {
    return {};
  }
}

function writeSlipEdits(map) {
  try {
    localStorage.setItem(SLIP_EDITS_KEY, JSON.stringify(map));
  } catch (err) {
    // storage blocked or full - the edit lives for this render only
  }
}

// The fixture's own first choice when it is added to a slip: the board ships in shelf order,
// and the model's safest in-band market is what "Add" puts on the slip.
function bestMarketCode(entry) {
  const markets = entry.markets || [];
  const pick = markets.find((m) => m.prob >= 0.7) || markets[0];
  return pick ? pick.code : "";
}

// One saved {match, selection, note} back into a slip leg, priced from the pool entry.
function legFromPool(editLeg, entry) {
  if (!entry) return null; // the fixture has left the feed - this leg is simply dropped
  const markets = entry.markets || [];
  const market = markets.find((m) => m.code === editLeg.s)
    || markets.find((m) => m.code === bestMarketCode(entry));
  if (!market) return null;
  return {
    match_key: entry.match_key,
    league: entry.league,
    competition: entry.competition,
    sport: entry.sport,
    kickoff_utc: entry.kickoff_utc,
    home_team: entry.home_team,
    away_team: entry.away_team,
    home_logo: entry.home_logo,
    away_logo: entry.away_logo,
    selection: market.code,
    market: market.market,
    label: market.label,
    group: market.group,
    prob: market.prob,
    odds: market.odds,
    odds_source: market.odds_source,
    insight: (entry.insights || {})[market.group] || "",
    note: editLeg.note || "",
  };
}

// The published slip with this device's edits applied - a copy, so the feed's own object is
// never mutated and "Reset" is always one delete away.
function applySlipEdit(slip) {
  const out = { ...slip, isLoaded: slip === loadedSlip };
  const edit = slip.code ? readSlipEdits()[slip.code] : null;
  if (!edit || !Array.isArray(edit.legs) || !edit.legs.length) return out;
  const index = poolIndex();
  const legs = edit.legs.map((leg) => legFromPool(leg, index.get(leg.m))).filter(Boolean);
  if (!legs.length) return out;
  const total = legs.reduce((product, leg) => product * (Number(leg.odds) || 1), 1);
  const winProb = legs.reduce((product, leg) => product * (Number(leg.prob) || 0), 1);
  out.legs = legs;
  out.total_odds = Math.round(total * 100) / 100;
  out.win_prob = Math.round(winProb * 10000) / 10000;
  out.edited = true;
  return out;
}

// The current legs of a slip as the editor stores them (whatever the render is showing).
function currentEditLegs(slip) {
  const existing = slip.code ? readSlipEdits()[slip.code] : null;
  if (existing && Array.isArray(existing.legs)) return existing.legs;
  return slip.legs.map((leg) => ({ m: leg.match_key, s: leg.selection, note: leg.note || "" }));
}

function saveSlipLegs(slip, legs) {
  if (!slip.code) return;
  const map = readSlipEdits();
  if (legs.length) map[slip.code] = { legs, updated_at: new Date().toISOString() };
  else delete map[slip.code]; // back to the published slip
  writeSlipEdits(map);
}

function editorLegHtml(leg, idx, index) {
  const entry = index.get(leg.match_key);
  const markets = entry ? entry.markets || [] : [];
  const insight = entry ? (entry.insights || {})[leg.group] || leg.insight : leg.insight;
  const options = markets.length
    ? markets.map((m) => `<option value="${esc(m.code)}"${m.code === leg.selection ? " selected" : ""}>${esc(m.market)} &middot; ${esc(m.label)} @ ${m.odds.toFixed(2)}</option>`).join("")
    : `<option selected>${esc(leg.label)} @ ${Number(leg.odds).toFixed(2)} (fixture no longer in the feed)</option>`;
  return `
      <li class="editor-leg">
        <div class="editor-leg-top">
          <span class="editor-leg-match">${esc(leg.home_team)}<span class="vs">vs</span>${esc(leg.away_team)}</span>
          <button class="editor-remove" data-edit-action="remove" data-leg-idx="${idx}" title="Take this fixture off the slip" aria-label="Remove ${esc(leg.home_team)} against ${esc(leg.away_team)}">&times;</button>
        </div>
        <select class="editor-market" data-edit-action="market" data-leg-idx="${idx}" aria-label="Bookmaker market for this fixture">${options}</select>
        <input class="editor-note" type="text" maxlength="240" placeholder="Your view on this game (kept on this device)" value="${esc(leg.note || "")}" data-edit-action="note" data-leg-idx="${idx}">
        <p class="editor-insight">${esc(insight || "")}</p>
      </li>`;
}

function candidateHtml(entry, index) {
  const text = `${entry.home_team} ${entry.away_team} ${entry.competition || entry.league || ""}`.toLowerCase();
  const best = (entry.markets || [])[0];
  return `
      <li class="editor-candidate" data-candidate="${esc(text)}">
        <span class="candidate-when">${esc(formatSlipKickoff(entry.kickoff_utc))}</span>
        <span class="candidate-match">${esc(entry.home_team)}<span class="vs">vs</span>${esc(entry.away_team)}</span>
        <span class="candidate-league">${esc(entry.competition || entry.league)}</span>
        <span class="candidate-pick">${best ? `${esc(best.label)} @ ${best.odds.toFixed(2)}` : ""}</span>
        <button class="slip-action" data-edit-action="add" data-entry-key="${esc(entry.match_key)}">Add</button>
      </li>`;
}

function slipEditorHtml(slip, index) {
  const open = Boolean(slip.code) && openSlipEditors.has(slip.code);
  // built only while open - a closed card renders its action bar and nothing else, so a
  // page of six slips does not carry six hidden candidate lists around
  if (!open || !slip.code || !poolSize()) return "";
  const entries = poolIndex();
  // only fixtures inside this slip's own window can be swapped in
  const windowEntry = ((slipsData && slipsData.filters) || []).find((f) => f.label === slip.window);
  const start = windowEntry ? Date.parse(windowEntry.start_utc) : -Infinity;
  const end = windowEntry ? Date.parse(windowEntry.end_utc) : Infinity;
  const chosen = new Set(slip.legs.map((leg) => leg.match_key));
  const candidates = ((slipsData && slipsData.pool) || []).filter((entry) => {
    const when = Date.parse(entry.kickoff_utc);
    return !chosen.has(entry.match_key) && when >= start && when < end;
  });
  const shown = candidates.slice(0, 30);
  return `
    <div class="slip-editor" data-editor-for="${esc(slip.code)}"${open ? "" : " hidden"}>
      <div class="slip-editor-head">
        <span class="slip-editor-title">Change this slip</span>
        <span class="slip-editor-total">Total <b>${Number(slip.total_odds).toFixed(2)}</b> &middot; win chance ${pct(slip.win_prob)}</span>
      </div>
      <ul class="editor-legs">${slip.legs.map((leg, i) => editorLegHtml(leg, i, entries)).join("")}</ul>
      <div class="editor-add">
        <label class="sr-only" for="editor-search-${esc(slip.code)}">Search fixtures to add</label>
        <input id="editor-search-${esc(slip.code)}" class="editor-search" type="search" placeholder="Search a team or league to add a fixture" data-editor-search="${esc(slip.code)}" autocomplete="off">
        <ul class="editor-candidates" data-candidates="${esc(slip.code)}">
          ${shown.map((entry) => candidateHtml(entry, entries)).join("")}
          ${candidates.length > shown.length ? `<li class="editor-more">${candidates.length - shown.length} more fixtures in this window - search above to narrow it down.</li>` : ""}
          ${!candidates.length ? `<li class="editor-more">Every fixture in this window is already on the slip.</li>` : ""}
        </ul>
      </div>
      <div class="slip-editor-foot">
        <button class="slip-action danger" data-edit-action="reset" data-slip-idx="${index}">Reset to the original slip</button>
        <span class="slip-editor-hint">Add or remove fixtures, switch the market, or write your own view - it stays on this device.</span>
      </div>
    </div>`;
}

function setUpSlipEditor() {
  const panel = document.getElementById("slips");

  panel.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-edit-action]");
    if (!btn) return;
    const action = btn.dataset.editAction;
    // actions inside the editor carry no slip index of their own - their card is the slip
    const slip = renderedSlips[Number(btn.dataset.slipIdx)] || renderedSlips[slipIndexOf(btn)];

    if (action === "toggle" && slip) {
      if (openSlipEditors.has(slip.code)) openSlipEditors.delete(slip.code);
      else openSlipEditors.add(slip.code);
      renderSlips();
      return;
    }
    if (action === "reset" && slip) {
      const map = readSlipEdits();
      delete map[slip.code];
      writeSlipEdits(map);
      renderSlips();
      return;
    }
    if (action === "remove" && slip) {
      const legs = currentEditLegs(slip);
      legs.splice(Number(btn.dataset.legIdx), 1);
      saveSlipLegs(slip, legs);
      renderSlips();
      return;
    }
    if (action === "add" && slip) {
      const entry = poolIndex().get(btn.dataset.entryKey);
      if (!entry) return;
      const legs = currentEditLegs(slip);
      if (legs.some((leg) => leg.m === entry.match_key)) return;
      legs.push({ m: entry.match_key, s: bestMarketCode(entry), note: "" });
      saveSlipLegs(slip, legs);
      renderSlips();
      return;
    }
    // the search box only narrows the list already on screen - no re-render, so the caret
    // never jumps while a team name is being typed
  });

  panel.addEventListener("change", (e) => {
    const select = e.target.closest('[data-edit-action="market"]');
    if (!select) return;
    const slip = renderedSlips[slipIndexOf(select)];
    if (!slip) return;
    const legs = currentEditLegs(slip);
    const idx = Number(select.dataset.legIdx);
    if (!legs[idx]) return;
    legs[idx] = { ...legs[idx], s: select.value };
    saveSlipLegs(slip, legs);
    renderSlips();
  });

  panel.addEventListener("input", (e) => {
    const note = e.target.closest('[data-edit-action="note"]');
    if (note) {
      const slip = renderedSlips[slipIndexOf(note)];
      if (!slip) return;
      const legs = currentEditLegs(slip);
      const idx = Number(note.dataset.legIdx);
      if (!legs[idx]) return;
      legs[idx] = { ...legs[idx], note: note.value };
      saveSlipLegs(slip, legs);
      return;
    }
    const search = e.target.closest("[data-editor-search]");
    if (!search) return;
    const query = search.value.trim().toLowerCase();
    const list = document.querySelector(`[data-candidates="${CSS.escape(search.dataset.editorSearch)}"]`);
    if (!list) return;
    for (const item of list.querySelectorAll(".editor-candidate")) {
      item.hidden = Boolean(query) && !item.dataset.candidate.includes(query);
    }
  });
}

// The editor controls live inside a slip card, so their nearest card is the slip they edit.
function slipIndexOf(node) {
  const card = node.closest(".slip-card");
  if (!card) return -1;
  const cards = [...document.querySelectorAll("#slips-content .slip-card")];
  return cards.indexOf(card);
}

function readSavedSlips() {
  try {
    const list = JSON.parse(localStorage.getItem(SAVED_SLIPS_KEY) || "[]");
    if (!Array.isArray(list)) return [];
    // a slip saved before the five-character codes existed carries only the long payload
    return list.map((s) => {
      const legacy = String(s.code || "").startsWith(BET_CODE_PREFIX);
      return { ...s, payload: s.payload || (legacy ? s.code : ""), code: legacy ? "" : String(s.code || "") };
    });
  } catch (err) {
    return [];
  }
}

function writeSavedSlips(list) {
  try {
    localStorage.setItem(SAVED_SLIPS_KEY, JSON.stringify(list.slice(0, 30)));
  } catch (err) {
    // storage blocked or full - the slips just do not persist
  }
}

function saveSlip(slip) {
  const payload = slip.code_payload || (String(slip.code || "").startsWith(BET_CODE_PREFIX) ? slip.code : "");
  if (!slip.code && !payload) return;
  const entry = {
    code: slip.code || "",
    payload: payload || "",
    window: slip.window || "",
    kind: slip.kind === "main" ? "main" : "combo",
    total_odds: slip.total_odds,
    legs: slip.legs.length,
    saved_at: new Date().toISOString(),
  };
  const rest = readSavedSlips().filter((s) => (entry.payload ? s.payload !== entry.payload : s.code !== entry.code));
  writeSavedSlips([entry, ...rest]);
}

// The saved slips are part of the same list as everything else - they are just the slips this
// device is holding on to, not a separate bet view.
function savedSlipsHtml() {
  const list = readSavedSlips();
  if (!list.length) return "";
  return `
    <h3 class="saved-slips-title">Saved slips <span class="saved-slips-count">${list.length}</span></h3>
    <p class="saved-slips-note">Kept on this device - a saved slip still opens after the day's slips have been rebuilt.</p>
    <ul class="saved-slips">
      ${list
        .map(
          (s, i) => `
        <li class="saved-slip">
          <span class="saved-slip-name"><code class="slip-code-value small">${esc(s.code || "\u00b7\u00b7\u00b7\u00b7\u00b7")}</code> ${s.kind === "main" ? "Main slip" : "Accumulator"} <span class="saved-slip-meta">${esc(s.window || "")} &middot; ${s.legs} legs &middot; total ${Number(s.total_odds || 0).toFixed(2)}</span></span>
          <span class="saved-slip-actions">
            <button class="slip-action" data-saved-action="load" data-saved-idx="${i}">Load</button>
            <button class="slip-action" data-saved-action="copy" data-saved-idx="${i}">Copy code</button>
            <button class="slip-action danger" data-saved-action="remove" data-saved-idx="${i}" aria-label="Delete this saved slip">&times;</button>
          </span>
        </li>`
        )
        .join("")}
    </ul>`;
}

// The slip a five-character code names, in whatever window it was built for.
function findPublishedSlip(code) {
  for (const f of (slipsData && slipsData.filters) || []) {
    if (f.main_slip && f.main_slip.code === code) return f.main_slip;
    const hit = (f.slips || []).find((s) => s.code === code);
    if (hit) return hit;
  }
  return null;
}

// Open the slip behind a code: our five characters, or a full PSB1- payload pasted from a
// saved slip. Throws with a sentence the page can show when nothing matches.
function openSlipByCode(text) {
  const raw = String(text || "").trim();
  if (!raw) throw new Error("Type a slip code first.");
  if (raw.toUpperCase().startsWith(BET_CODE_PREFIX)) {
    const slip = decodeBetCode(raw);
    slip.code_payload = raw;
    loadedSlip = slip;
    return slip;
  }
  const code = raw.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (code.length !== CODE_LENGTH || [...code].some((c) => !CODE_ALPHABET.includes(c))) {
    throw new Error(`A slip code is ${CODE_LENGTH} characters - letters and digits, no 0, 1, O or I. Check the one you copied.`);
  }
  const published = findPublishedSlip(code);
  if (published) {
    loadedSlip = published;
    return published;
  }
  const saved = readSavedSlips().find((s) => s.code === code && s.payload);
  if (saved) {
    const slip = decodeBetCode(saved.payload);
    slip.code = code;
    slip.code_payload = saved.payload;
    loadedSlip = slip;
    return slip;
  }
  throw new Error(`No slip in the current feed has the code ${code}. Codes name the slips built for the current window, so an older one stops resolving - save a slip to keep it on this device.`);
}

function setSlipCodeError(message) {
  const box = document.getElementById("betcode-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function setUpSlipCodes() {
  document.getElementById("slips").addEventListener("click", async (e) => {
    const actionBtn = e.target.closest("[data-slip-action]");
    if (actionBtn) {
      const slip = renderedSlips[Number(actionBtn.dataset.slipIdx)];
      if (!slip) return;
      const action = actionBtn.dataset.slipAction;
      if (action === "copy-code") {
        flashButton(actionBtn, (await copyText(slip.code || "")) ? "Copied \u2713" : "Copy failed");
      } else if (action === "copy-text") {
        flashButton(actionBtn, (await copyText(slipPlainText(slip))) ? "Copied \u2713" : "Copy failed");
      } else if (action === "save") {
        saveSlip(slip);
        renderSlips(); // the saved slip appears in the same list, right below
      } else if (action === "close") {
        loadedSlip = null;
        renderSlips();
      }
      return;
    }
    const savedBtn = e.target.closest("[data-saved-action]");
    if (!savedBtn) return;
    const idx = Number(savedBtn.dataset.savedIdx);
    const list = readSavedSlips();
    const entry = list[idx];
    if (!entry) return;
    const action = savedBtn.dataset.savedAction;
    if (action === "copy") {
      flashButton(savedBtn, (await copyText(entry.code || entry.payload || "")) ? "Copied \u2713" : "Copy failed");
    } else if (action === "load") {
      try {
        openSlipByCode(entry.payload || entry.code);
        setSlipCodeError("");
        renderSlips();
      } catch (err) {
        setSlipCodeError(err.message);
      }
    } else if (action === "remove") {
      writeSavedSlips(list.filter((_, i) => i !== idx));
      renderSlips();
    }
  });

  // Typing a code is the other half of the same list: the slip it names opens at the top.
  const input = document.getElementById("betcode-input");
  const load = () => {
    try {
      openSlipByCode(input.value);
      setSlipCodeError("");
      input.value = "";
      renderSlips();
      // the slip is already on screen by now - scrolling is a nicety, so a browser without
      // scrollIntoView must not turn a successful load into an error message
      const content = document.getElementById("slips-content");
      if (typeof content.scrollIntoView === "function") content.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      setSlipCodeError(err.message);
    }
  };
  document.getElementById("betcode-load").addEventListener("click", load);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") load();
  });
}

function renderSlipFilters(filters) {
  document.getElementById("slip-filters").innerHTML = filters
    .map((f) => {
      const n = f.slips.length + (f.main_slip ? 1 : 0);
      return `
      <button class="chip ${f.id === slipFilter ? "active" : ""}" data-slip-filter="${esc(f.id)}" aria-pressed="${f.id === slipFilter}">
        ${esc(f.label)} <span class="chip-count">${n}</span>
      </button>`;
    })
    .join("");
}

function renderSlips() {
  const content = document.getElementById("slips-content");
  if (!slipsData) return;
  const filters = slipsData.filters || [];
  if (!filters.length) {
    document.getElementById("slip-filters").innerHTML = "";
    content.innerHTML = `<div class="empty-state">No slips are available yet - the next scheduled refresh will build them.</div>`;
    return;
  }
  if (!slipFilter || !filters.some((f) => f.id === slipFilter)) slipFilter = filters[0].id;
  renderSlipFilters(filters);

  const active = filters.find((f) => f.id === slipFilter);
  // A slip opened by code is shown once, at the top - not a second time in its own window.
  const rawMain = active.main_slip === loadedSlip ? null : active.main_slip;
  const rawCombos = active.slips.filter((s) => s !== loadedSlip);
  // every card is rendered with this device's edits applied - swapped fixtures, switched
  // markets, its own notes - and the totals recomputed from what is actually on the slip
  const loaded = loadedSlip ? applySlipEdit(loadedSlip) : null;
  const main = rawMain ? applySlipEdit(rawMain) : null;
  const combos = rawCombos.map(applySlipEdit);
  renderedSlips = [...(loaded ? [loaded] : []), ...(main ? [main] : []), ...combos];
  const legs = renderedSlips.reduce((n, slip) => n + slip.legs.length, 0);
  const liveLegs = renderedSlips.reduce((n, slip) => n + slip.legs.filter((l) => l.odds_source === "1xlite").length, 0);
  const note = legs
    ? `${liveLegs ? `${liveLegs}/${legs} legs priced live from 1xLite` : "Prices are the model's fair odds (1 / probability) - the betting app couldn't be reached"}. Day window ${formatSlipDate(active.start_utc)} to ${formatSlipDate(active.end_utc)} (Kathmandu time).`
    : `Day window ${formatSlipDate(active.start_utc)} to ${formatSlipDate(active.end_utc)} (Kathmandu time).`;
  const card = (slip, title, cls = "") => slipCardHtml(slip, renderedSlips.indexOf(slip), title, cls);
  const top = [
    loaded ? card(loaded, `Loaded slip <span class="slip-badge">Loaded</span>`, "loaded") : "",
    main ? card(main, `Main slip <span class="slip-badge">Best bet</span>`, "main") : "",
  ].join("");
  const grid = combos.map((s, i) => card(s, `Slip ${i + 1}`)).join("");
  content.innerHTML = `
    <h2 class="day-heading">Prediction slips <span class="day-total">${esc(active.label)}</span></h2>
    <p class="view-note"><b>Main slip:</b> our surest picks, every leg rated at least 75% likely and priced 1.10&ndash;1.30, totalling 2.00&ndash;3.50. <b>Combination slips:</b> up to 5 of them, every leg rated at least 70% likely, each totalling 2.00&ndash;3.00 - and when the window holds no fixture that strong, a slip is left unbuilt rather than padded with a weaker pick. Every leg carries the reason it was chosen; <b>Change slip</b> lets you swap fixtures, switch the bookmaker market and write your own view. ${esc(note)}</p>
    ${
      renderedSlips.length
        ? `${top ? `<div class="slip-main">${top}</div>` : ""}${grid ? `<div class="slip-grid">${grid}</div>` : ""}`
        : `<div class="empty-state">Not enough qualifying matches in this window yet to build a slip - try a wider one.</div>`
    }
    ${savedSlipsHtml()}
  `;
}

async function loadSlips() {
  const content = document.getElementById("slips-content");
  try {
    slipsData = await getJson("data/slips.json");
    renderSlips();
  } catch (err) {
    if (!slipsData) content.innerHTML = `<div class="empty-state">Couldn't load slips: ${esc(err.message)}</div>`;
  }
}

function setUpSlipFilters() {
  document.getElementById("slip-filters").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-slip-filter]");
    if (!chip || chip.dataset.slipFilter === slipFilter) return;
    slipFilter = chip.dataset.slipFilter;
    renderSlips();
  });
}

setUpTabs();
setUpModal();
setUpDelegatedClicks();
setUpHeaderScroll();
setUpViewToggle();
setUpFilters();
setUpCountryDateSelects();
setUpSlipFilters();
setUpSlipCodes();
setUpSlipEditor();
setUpAutoRefresh();
loadUpcoming();
loadSlips();
loadScoreboard();
loadHistory();
