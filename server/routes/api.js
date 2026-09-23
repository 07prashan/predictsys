const express = require("express");
const db = require("../db");
const { getCachedLogo } = require("../logos");
const { LEAGUE_NAMES, countryOf, competitionOf } = require("../leagues");

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
    league_name: LEAGUE_NAMES[row.league] || row.league,
    country: countryOf(row.league),
    competition: competitionOf(row.league),
    // undefined (never looked up) and null (looked up, not found) both mean
    // "no logo yet" to the frontend - only a real cached URL is worth sending
    home_logo: getCachedLogo(row.home_team) || null,
    away_logo: getCachedLogo(row.away_team) || null,
  };
}

// Upcoming fixtures we've predicted but that haven't been played yet.
router.get("/predictions", (req, res) => {
  const rows = db
    .prepare(
      `SELECT league, match_date, home_team, away_team,
              prob_home, prob_draw, prob_away, predicted_outcome,
              expected_goals_home, expected_goals_away,
              correct_score_home, correct_score_away,
              btts_yes_prob, over_2_5_prob, markets_json,
              best_pick_market, best_pick_label, best_pick_prob
       FROM predictions
       WHERE actual_outcome IS NULL
       ORDER BY match_date ASC`
    )
    .all();
  res.json(rows.map(withExtras));
});

// Predictions that have since been graded against the real result.
router.get("/predictions/history", (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 100, 500);
  const rows = db
    .prepare(
      `SELECT league, match_date, home_team, away_team,
              prob_home, prob_draw, prob_away, predicted_outcome, actual_outcome, correct,
              correct_score_home, correct_score_away, btts_yes_prob, over_2_5_prob, markets_json,
              actual_home_goals, actual_away_goals,
              best_pick_market, best_pick_label, best_pick_prob
       FROM predictions
       WHERE actual_outcome IS NOT NULL
       ORDER BY match_date DESC
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
