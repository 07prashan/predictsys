const express = require("express");
const db = require("../db");
const { logoFor } = require("../logo_lookup");
const { upcomingFilter } = require("../upcoming");
const { LEAGUE_NAMES, competitionOf, groupOf } = require("../leagues");

const router = express.Router();

function withExtras(row) {
  const { markets_json, ...rest } = row;
  let extraMarkets = {};
  try {
    extraMarkets = markets_json ? JSON.parse(markets_json) : {};
  } catch {
    extraMarkets = {};
  }
  return {
    ...rest,
    ...extraMarkets,
    // rows from before multi-sport support have no sport - they're all club football
    sport: row.sport || "football",
    // the specific competition/tournament where there is one (a Nations League game, a
    // named tennis event), else the league's own name
    competition: row.competition || competitionOf(row.league),
    league_name: row.competition || LEAGUE_NAMES[row.league] || row.league,
    country: groupOf(row.league),
    // null means "no logo yet" to the frontend, which then draws an initials badge
    home_logo: logoFor(row.home_team),
    away_logo: logoFor(row.away_team),
  };
}

// Upcoming matches across every sport: not yet settled, and not already over.
router.get("/predictions", (req, res) => {
  const filter = upcomingFilter();
  const rows = db
    .prepare(
      `SELECT league, sport, competition, round, match_date, kickoff_utc, home_team, away_team,
              prob_home, prob_draw, prob_away, predicted_outcome,
              expected_goals_home, expected_goals_away,
              correct_score_home, correct_score_away,
              btts_yes_prob, over_2_5_prob, markets_json,
              best_pick_market, best_pick_label, best_pick_prob
       FROM predictions
       WHERE ${filter.sql}
       ORDER BY COALESCE(kickoff_utc, match_date) ASC`
    )
    .all(filter.params);
  res.set("Cache-Control", "public, max-age=30");
  res.json(rows.map(withExtras));
});

// Predictions that have since been graded against the real result.
router.get("/predictions/history", (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 100, 500);
  const rows = db
    .prepare(
      `SELECT league, sport, competition, round, match_date, kickoff_utc, home_team, away_team,
              prob_home, prob_draw, prob_away, predicted_outcome, actual_outcome, correct,
              correct_score_home, correct_score_away, btts_yes_prob, over_2_5_prob, markets_json,
              actual_home_goals, actual_away_goals,
              best_pick_market, best_pick_label, best_pick_prob
       FROM predictions
       WHERE actual_outcome IS NOT NULL
       ORDER BY COALESCE(kickoff_utc, match_date) DESC
       LIMIT ?`
    )
    .all(limit);
  res.json(rows.map(withExtras));
});

// Accuracy / log-loss per league, and overall, over every settled prediction ever made.
router.get("/scoreboard", (req, res) => {
  const rows = db
    .prepare(
      `SELECT league, prob_home, prob_draw, prob_away, actual_outcome, correct
       FROM predictions
       WHERE actual_outcome IS NOT NULL`
    )
    .all();

  if (rows.length === 0) {
    return res.json([]);
  }

  const probOfActual = (r) => ({ H: r.prob_home, D: r.prob_draw, A: r.prob_away }[r.actual_outcome]);

  const summarize = (group) => {
    const n = group.length;
    const accuracy = group.reduce((sum, r) => sum + r.correct, 0) / n;
    const logLoss = -group.reduce((sum, r) => sum + Math.log(Math.max(probOfActual(r), 1e-10)), 0) / n;
    return { n_settled: n, accuracy, log_loss: logLoss };
  };

  const byLeague = new Map();
  for (const row of rows) {
    if (!byLeague.has(row.league)) byLeague.set(row.league, []);
    byLeague.get(row.league).push(row);
  }

  const result = [...byLeague.entries()].map(([league, group]) => ({
    league,
    league_name: LEAGUE_NAMES[league] || league,
    ...summarize(group),
  }));
  result.push({ league: "ALL", league_name: "All leagues", ...summarize(rows) });

  res.json(result);
});

module.exports = router;
