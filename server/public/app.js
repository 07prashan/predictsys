const OUTCOME_LABEL = { H: "Home", D: "Draw", A: "Away" };
const matchDataByKey = new Map();

// How many days (today included) the day strip and "this week" scaffold cover. The pipeline
// predicts ten days ahead (predict.py's HORIZON_DAYS) - the two should move together.
const DAYS_AHEAD = 10;

// Minutes after kickoff at which a match counts as over and is dropped from the page for
// good. The server filters coarsely (a few hours) in UTC; this applies the real per-sport
// cutoff against the viewer's own clock, and keeps applying it while the page stays open.
const FINISHED_AFTER_MIN = { football: 150, tennis: 360 };

const CATEGORIES = [
  { id: "all", label: "All sports" },
  { id: "leagues", label: "Football · Leagues" },
  { id: "intl", label: "Football · National teams" },
  { id: "atp", label: "Tennis · ATP" },
  { id: "wta", label: "Tennis · WTA" },
];
const CATEGORY_ORDER = { leagues: 0, intl: 1, atp: 2, wta: 3 };

function categoryOf(m) {
  if (m.sport === "tennis") return m.league === "WTA" ? "wta" : "atp";
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

function outcomeText(m, code) {
  if (isTennis(m)) return code === "H" ? m.home_team : m.away_team;
  return OUTCOME_LABEL[code];
}

function probBar(row) {
  if (isTennis(row)) {
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
  return [...groups.values()]
    .map((g) => ({ ...g, matches: g.matches.sort(byKickoff) }))
    .sort((a, b) => CATEGORY_ORDER[a.category] - CATEGORY_ORDER[b.category] || byKickoff(a.matches[0], b.matches[0]));
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

function groupByCountry(rows) {
  const byCountry = new Map();
  for (const row of rows) {
    if (!byCountry.has(row.country)) byCountry.set(row.country, new Map());
    const byComp = byCountry.get(row.country);
    const key = `${row.league}|${row.competition}`;
    if (!byComp.has(key)) byComp.set(key, { key, competition: row.competition, leagueCode: row.league, matches: [] });
    byComp.get(key).matches.push(row);
  }
  // football-data.co.uk's own league codes already rank tiers (E0 = top flight, E1 = second
  // tier, ...), so sorting by code puts a country's leagues in tier order with no ranking table
  return [...byCountry.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([country, byComp]) => ({
      country,
      competitions: [...byComp.values()]
        .map((c) => ({ ...c, matches: c.matches.sort(byKickoff) }))
        .sort((a, b) => (a.leagueCode < b.leagueCode ? -1 : a.leagueCode > b.leagueCode ? 1 : a.competition.localeCompare(b.competition))),
    }));
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
    const rows = await fetch("/api/predictions").then((r) => {
      if (!r.ok) throw new Error(`server answered ${r.status}`);
      return r.json();
    });
    upcomingRows = prepare(rows);
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
    if (!document.hidden) renderUpcoming(false);
  }, 60 * 1000);
  setInterval(() => {
    if (!document.hidden) loadUpcoming();
  }, 5 * 60 * 1000);
}

// ---- Track record ----

async function loadScoreboard() {
  const el = document.getElementById("scoreboard-content");
  try {
    const rows = await fetch("/api/scoreboard").then((r) => r.json());
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
  return isTennis(r) || r.league === "INT" ? r.competition : r.league;
}

async function loadHistory() {
  const el = document.getElementById("history-content");
  try {
    const rows = prepare(await fetch("/api/predictions/history?limit=100").then((r) => r.json()));
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

function formLine(matches) {
  if (!matches.length) return `<p class="empty-state">No recent matches on record.</p>`;
  return `<ul class="form-list">${matches
    .map(
      (m) => `
    <li>
      <span class="form-badge ${m.outcome}">${m.outcome}</span>
      <span class="form-date">${formatDate(m.date)}</span>
      <span class="form-opponent">${m.venue === "H" ? "vs" : "@"} ${teamWithLogo(m.opponent, m.opponent_logo, "")}</span>
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
    <div class="detail-market-grid">${tennis ? tennisMarkets(m) : footballMarkets(m)}</div>

    <p class="section-title">Head-to-head</p>
    <div id="h2h-slot" class="loading">Loading head-to-head history...</div>
  `;
  showModal();

  try {
    const h2h = await fetch(
      `/api/h2h?home=${encodeURIComponent(m.home_team)}&away=${encodeURIComponent(m.away_team)}&league=${encodeURIComponent(m.league)}`
    ).then((r) => r.json());
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
    const query = league ? `?league=${encodeURIComponent(league)}` : "";
    const t = await fetch(`/api/team/${encodeURIComponent(teamName)}${query}`).then((r) => r.json());
    if (t.error) {
      content.innerHTML = `<p class="empty-state">${esc(t.error)}</p>`;
      return;
    }
    const tennis = t.sport === "tennis";
    const rankLabel = tennis ? "of active players by rating" : "of " + t.league_size + " by rating";
    const lastStat = tennis
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
      ${formLine(t.recent_matches)}

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

setUpTabs();
setUpModal();
setUpDelegatedClicks();
setUpHeaderScroll();
setUpViewToggle();
setUpFilters();
setUpCountryDateSelects();
setUpAutoRefresh();
loadUpcoming();
loadScoreboard();
loadHistory();
