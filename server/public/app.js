const OUTCOME_LABEL = { H: "Home", D: "Draw", A: "Away" };
const matchDataByKey = new Map();

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
  const safeInitials = initials(name);
  if (!logoUrl) return `<span class="team-logo team-logo-fallback">${safeInitials}</span>`;
  return `<img class="team-logo" src="${logoUrl}" alt="" loading="lazy"
    onerror="this.outerHTML='&lt;span class=&quot;team-logo team-logo-fallback&quot;&gt;${safeInitials}&lt;/span&gt;'">`;
}

// data-team + the clickable class are what let event delegation open a team
// profile instead of the match it's part of - see setUpDelegatedClicks().
// role="button" + tabindex make it keyboard-reachable too, not just mouse-clickable.
function teamWithLogo(name, logoUrl) {
  return `<span class="team clickable" data-team="${name}" role="button" tabindex="0" aria-label="View ${name} team profile">${teamLogo(name, logoUrl)}<span class="team-name-text">${name}</span></span>`;
}

function probBar(row) {
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
  if (diff === -1) return `Yesterday · ${full}`;
  if (diff < -1 && diff >= -7) return `${full} (${-diff} days ago)`;
  return full;
}

// Groups predictions by calendar date rather than league: a "this week" scaffold
// of today + the next 6 days always renders (even on days with no fixtures, so
// "today" is never silently missing), followed by anything further ahead that
// didn't fit that window. A match_date before today means the game has already
// been played - the source just hasn't posted a result to settle it yet - so
// those are dropped entirely rather than shown; there's nothing upcoming left
// to say about a finished game.
// Today + the next 6 days as {date, label} - shared by the "This week" scaffold
// and by each competition's date dropdown in the country view, so both agree on
// exactly which days count as "this week".
function buildWeekDates(from) {
  const days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(from);
    d.setDate(d.getDate() + i);
    days.push({ date: localDateKey(d), label: dayLabel(localDateKey(d), from) });
  }
  return days;
}

function groupByDate(rows) {
  const from = todayLocal();
  const upcoming = rows.filter((row) => daysBetween(row.match_date, from) >= 0);

  const byDate = new Map();
  for (const row of upcoming) {
    if (!byDate.has(row.match_date)) byDate.set(row.match_date, []);
    byDate.get(row.match_date).push(row);
  }

  const week = buildWeekDates(from).map(({ date, label }) => {
    const matches = byDate.get(date) || [];
    byDate.delete(date);
    return { date, label, matches };
  });

  const later = [...byDate.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, matches]) => ({ date, label: dayLabel(date, from), matches }));

  return { week, later };
}

// Same upcoming-only filter as groupByDate, but bucketed by country and then by
// competition within it. football-data.co.uk's own league codes already rank
// tiers (E0 = top flight, E1 = second tier, ...), so sorting competitions by
// code puts each country's leagues in tier order for free, with no separate
// ranking table to keep in sync as more competitions are added.
function groupByCountry(rows) {
  const from = todayLocal();
  const upcoming = rows.filter((row) => daysBetween(row.match_date, from) >= 0);
  const weekDates = buildWeekDates(from);

  const byCountry = new Map();
  for (const row of upcoming) {
    const country = row.country || row.league_name;
    if (!byCountry.has(country)) byCountry.set(country, new Map());
    const byLeague = byCountry.get(country);
    if (!byLeague.has(row.league)) byLeague.set(row.league, []);
    byLeague.get(row.league).push(row);
  }

  const countries = [...byCountry.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([country, byLeague]) => ({
      country,
      competitions: [...byLeague.entries()]
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([leagueCode, matches]) => ({
          leagueCode,
          competition: matches[0].competition || matches[0].league_name,
          matches: matches.sort((a, b) => (a.match_date < b.match_date ? -1 : a.match_date > b.match_date ? 1 : 0)),
        })),
    }));

  return { weekDates, countries };
}

function matchesForLeagueOnDate(leagueCode, date) {
  return upcomingRows.filter((m) => m.league === leagueCode && m.match_date === date);
}

function competitionMatchesHtml(matches) {
  return matches.length
    ? matches.map((m) => matchCard(m)).join("")
    : `<div class="empty-state small">No fixtures scheduled yet.</div>`;
}

// Each competition gets its own date dropdown (defaulting to today) rather than
// showing every upcoming day at once - the dropdown only ever re-renders this one
// competition's match-grid (see setUpCountryDateSelects), never refetching.
function competitionBlockHtml({ leagueCode, competition }, weekDates) {
  const defaultDate = weekDates[0].date;
  const options = weekDates.map((d) => `<option value="${d.date}">${d.label}</option>`).join("");
  return `
    <div class="competition-block">
      <div class="competition-header">
        <h3>${competition}</h3>
        <select class="date-select" data-league="${leagueCode}" aria-label="Date for ${competition}">
          ${options}
        </select>
      </div>
      <div class="match-grid" data-league-matches="${leagueCode}">
        ${competitionMatchesHtml(matchesForLeagueOnDate(leagueCode, defaultDate))}
      </div>
    </div>
  `;
}

function countryGroupHtml({ country, competitions }, weekDates) {
  return `
    <div class="league-group" data-country="${country}">
      <h2>${country}</h2>
      ${competitions.map((c) => competitionBlockHtml(c, weekDates)).join("")}
    </div>
  `;
}

// Delegated once on the container, since #upcoming-content's innerHTML (and every
// .date-select inside it) is replaced wholesale on each render.
function setUpCountryDateSelects() {
  document.getElementById("upcoming-content").addEventListener("change", (e) => {
    const select = e.target.closest(".date-select");
    if (!select) return;
    const leagueCode = select.dataset.league;
    const grid = document.querySelector(`[data-league-matches="${leagueCode}"]`);
    if (!grid) return;
    grid.innerHTML = competitionMatchesHtml(matchesForLeagueOnDate(leagueCode, select.value));
  });
}

function leagueTag(m) {
  return `<span class="league-tag">${m.league_name}</span>`;
}

function marketChip(label, value) {
  return `<div class="market-chip"><span class="market-label">${label}</span><span class="market-value">${value}</span></div>`;
}

function matchKey(m) {
  return `${m.league}|${m.match_date}|${m.home_team}|${m.away_team}`;
}

function matchCard(m) {
  matchDataByKey.set(matchKey(m), m);
  return `
    <article class="match-card clickable" data-match-key="${matchKey(m)}" role="button" tabindex="0" aria-label="View match details: ${m.home_team} vs ${m.away_team}">
      <div class="match-card-top">
        ${leagueTag(m)}
        <span class="best-pick" title="Most confident market for this match">★ ${m.best_pick_label} (${pct(m.best_pick_prob)})</span>
      </div>
      <div class="match-teams">
        ${teamWithLogo(m.home_team, m.home_logo)}
        <span class="vs">vs</span>
        ${teamWithLogo(m.away_team, m.away_logo)}
      </div>
      <div class="match-result-row">
        ${probBar(m)}
        <span class="outcome-tag ${m.predicted_outcome}">${OUTCOME_LABEL[m.predicted_outcome]}</span>
      </div>
      <div class="market-row">
        ${marketChip("Correct Score", `${m.correct_score_home}-${m.correct_score_away}`)}
        ${marketChip("BTTS", m.btts_yes_prob >= 0.5 ? `Yes ${pct(m.btts_yes_prob)}` : `No ${pct(1 - m.btts_yes_prob)}`)}
        ${marketChip("Total Goals", m.over_2_5_prob >= 0.5 ? `Over 2.5 (${pct(m.over_2_5_prob)})` : `Under 2.5 (${pct(1 - m.over_2_5_prob)})`)}
      </div>
    </article>
  `;
}

function dayGroupHtml({ date, label, matches }) {
  const body = matches.length
    ? `<div class="match-grid">${matches.map((m) => matchCard(m)).join("")}</div>`
    : `<div class="empty-state small">No fixtures scheduled yet.</div>`;
  return `<div class="league-group" data-date="${date}"><h2>${label}</h2>${body}</div>`;
}

let upcomingRows = [];
let upcomingView = "date";

// Re-renders whatever's already been fetched under the current view mode -
// switching "By Date" / "By Country" is instant and never re-hits the API.
function renderUpcoming() {
  const el = document.getElementById("upcoming-content");

  if (upcomingView === "country") {
    const { weekDates, countries } = groupByCountry(upcomingRows);
    el.innerHTML = countries.length
      ? countries.map((c) => countryGroupHtml(c, weekDates)).join("")
      : `<div class="empty-state">No upcoming fixtures scheduled yet.</div>`;
    return;
  }

  const { week, later } = groupByDate(upcomingRows);
  const laterHtml = later.length
    ? `
      <p class="section-title">Further ahead</p>
      ${later.map(dayGroupHtml).join("")}
    `
    : "";

  el.innerHTML = `
    <p class="section-title">This week</p>
    ${week.map(dayGroupHtml).join("")}
    ${laterHtml}
  `;
}

async function loadUpcoming() {
  const el = document.getElementById("upcoming-content");
  try {
    upcomingRows = await fetch("/api/predictions").then((r) => r.json());
    renderUpcoming();
  } catch (err) {
    el.innerHTML = `<div class="empty-state">Couldn't load predictions: ${err.message}</div>`;
  }
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
        <h3>${r.league_name}</h3>
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
    el.innerHTML = `<div class="empty-state">Couldn't load the scoreboard: ${err.message}</div>`;
  }
}

async function loadHistory() {
  const el = document.getElementById("history-content");
  try {
    const rows = await fetch("/api/predictions/history?limit=100").then((r) => r.json());
    if (rows.length === 0) return;
    el.innerHTML = `
      <h2>Recent graded predictions</h2>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>Date</th><th>League</th><th>Match</th><th>Predicted</th><th>Actual</th><th>Score</th><th>Result</th></tr>
          </thead>
          <tbody>
            ${rows
              .map((r) => {
                matchDataByKey.set(matchKey(r), r);
                return `
              <tr class="clickable" data-match-key="${matchKey(r)}" tabindex="0" aria-label="View match details: ${r.home_team} vs ${r.away_team}">
                <td>${formatDate(r.match_date)}</td>
                <td>${r.league}</td>
                <td class="matchup">${teamWithLogo(r.home_team, r.home_logo)}<span class="vs">vs</span>${teamWithLogo(r.away_team, r.away_logo)}</td>
                <td><span class="outcome-tag ${r.predicted_outcome}">${OUTCOME_LABEL[r.predicted_outcome]}</span></td>
                <td><span class="outcome-tag ${r.actual_outcome}">${OUTCOME_LABEL[r.actual_outcome]}</span></td>
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
    el.innerHTML = `<div class="empty-state">Couldn't load prediction history: ${err.message}</div>`;
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
      <span class="form-opponent">${m.venue === "H" ? "vs" : "@"} ${teamWithLogo(m.opponent, m.opponent_logo)}</span>
      <span class="form-score">${m.goals_for}-${m.goals_against}</span>
    </li>
  `
    )
    .join("")}</ul>`;
}

async function renderMatchDetail(m) {
  const content = document.getElementById("modal-content");
  content.innerHTML = `
    <div class="modal-header-teams">
      ${teamWithLogo(m.home_team, m.home_logo)}
      <span class="vs">vs</span>
      ${teamWithLogo(m.away_team, m.away_logo)}
    </div>
    <div class="modal-subtitle">${m.league_name} &middot; ${formatDate(m.match_date)}</div>

    <div class="match-result-row" style="justify-content:center">
      ${probBar(m)}
      <span class="outcome-tag ${m.predicted_outcome}">${OUTCOME_LABEL[m.predicted_outcome]}</span>
    </div>

    <p class="section-title">All markets</p>
    <div class="detail-market-grid">
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
    </div>

    <p class="section-title">Head-to-head</p>
    <div id="h2h-slot" class="loading">Loading head-to-head history...</div>
  `;
  showModal();

  try {
    const h2h = await fetch(`/api/h2h?home=${encodeURIComponent(m.home_team)}&away=${encodeURIComponent(m.away_team)}`).then((r) =>
      r.json()
    );
    const slot = document.getElementById("h2h-slot");
    if (h2h.total === 0) {
      slot.innerHTML = `<p class="empty-state">These two haven't met in our records.</p>`;
      return;
    }
    slot.innerHTML = `
      <div class="h2h-summary">
        <div class="stat"><div class="num">${h2h.summary.home_wins}</div><div class="lbl">${m.home_team} wins</div></div>
        <div class="stat"><div class="num">${h2h.summary.draws}</div><div class="lbl">Draws</div></div>
        <div class="stat"><div class="num">${h2h.summary.away_wins}</div><div class="lbl">${m.away_team} wins</div></div>
      </div>
      <ul class="h2h-list">
        ${h2h.meetings
          .slice(0, 10)
          .map(
            (g) => `
          <li>
            <span class="h2h-date">${formatDate(g.date)}</span>
            <span>${g.home_team} vs ${g.away_team}</span>
            <span class="h2h-score">${g.home_goals}-${g.away_goals}</span>
          </li>
        `
          )
          .join("")}
      </ul>
    `;
  } catch (err) {
    document.getElementById("h2h-slot").innerHTML = `<p class="empty-state">Couldn't load head-to-head: ${err.message}</p>`;
  }
}

// ---- Modal: team profile ----

async function renderTeamProfile(teamName) {
  const content = document.getElementById("modal-content");
  content.innerHTML = `<div class="loading">Loading ${teamName}...</div>`;
  showModal();

  try {
    const t = await fetch(`/api/team/${encodeURIComponent(teamName)}`).then((r) => r.json());
    if (t.error) {
      content.innerHTML = `<p class="empty-state">${t.error}</p>`;
      return;
    }
    content.innerHTML = `
      <div class="modal-header-teams">
        ${teamLogo(t.team, t.logo)}
      </div>
      <div class="modal-subtitle" style="font-size:1.1rem;font-weight:700;color:var(--text)">${t.team}</div>
      <div class="modal-subtitle">${t.league_name}</div>

      <div class="profile-stats">
        <div class="stat"><div class="num">#${t.league_rank}</div><div class="lbl">of ${t.league_size} by rating</div></div>
        <div class="stat"><div class="num">${Math.round(t.elo_rating)}</div><div class="lbl">Elo rating</div></div>
        <div class="stat"><div class="num">${t.recent_form_ppg !== null ? t.recent_form_ppg.toFixed(2) : "-"}</div><div class="lbl">Pts/game (last 10)</div></div>
      </div>

      <p class="section-title">Recent form</p>
      ${formLine(t.recent_matches)}

      ${
        t.next_fixture
          ? `<p class="next-fixture-note">Next: ${t.next_fixture.home_team} vs ${t.next_fixture.away_team} on ${formatDate(t.next_fixture.match_date)}</p>`
          : ""
      }
    `;
  } catch (err) {
    content.innerHTML = `<p class="empty-state">Couldn't load ${teamName}: ${err.message}</p>`;
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
  if (teamEl) return renderTeamProfile(teamEl.dataset.team);
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
      renderTeamProfile(teamEl.dataset.team);
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
setUpCountryDateSelects();
loadUpcoming();
loadScoreboard();
loadHistory();
