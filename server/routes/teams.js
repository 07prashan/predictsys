const express = require("express");
const db = require("../db");
const { getCachedLogo } = require("../logos");
const { LEAGUE_NAMES } = require("../leagues");

const router = express.Router();

function pointsFor(team, result, home) {
  if (result === "D") return 1;
  const teamWon = (home && result === "H") || (!home && result === "A");
  return teamWon ? 3 : 0;
}

// Team profile: current rating + rank, recent form, next fixture.
router.get("/team/:name", (req, res) => {
  const name = req.params.name;

  const ratingRow = db.prepare("SELECT * FROM team_ratings WHERE team = ?").get(name);
  if (!ratingRow) {
    return res.status(404).json({ error: `No data for team "${name}"` });
  }

  const leagueRatings = db
    .prepare("SELECT team, elo_rating FROM team_ratings WHERE league = ? ORDER BY elo_rating DESC")
    .all(ratingRow.league);
  const rank = leagueRatings.findIndex((r) => r.team === name) + 1;

  const recentMatches = db
    .prepare(
      `SELECT * FROM matches WHERE home_team = ? OR away_team = ? ORDER BY date DESC LIMIT 10`
    )
    .all(name, name)
    .map((m) => {
      const isHome = m.home_team === name;
      const opponent = isHome ? m.away_team : m.home_team;
      const goalsFor = isHome ? m.home_goals : m.away_goals;
      const goalsAgainst = isHome ? m.away_goals : m.home_goals;
      const points = pointsFor(name, m.result, isHome);
      return {
        date: m.date,
        opponent,
        opponent_logo: getCachedLogo(opponent) || null,
        venue: isHome ? "H" : "A",
        goals_for: goalsFor,
        goals_against: goalsAgainst,
        outcome: points === 3 ? "W" : points === 1 ? "D" : "L",
      };
    });

  const formPpg = recentMatches.length
    ? recentMatches.reduce((sum, m) => sum + ({ W: 3, D: 1, L: 0 }[m.outcome]), 0) / recentMatches.length
    : null;

  const nextFixture = db
    .prepare(
      `SELECT league, match_date, home_team, away_team, prob_home, prob_draw, prob_away, predicted_outcome
       FROM predictions
       WHERE (home_team = ? OR away_team = ?) AND actual_outcome IS NULL
       ORDER BY match_date ASC LIMIT 1`
    )
    .get(name, name);

  res.json({
    team: name,
    league: ratingRow.league,
    league_name: LEAGUE_NAMES[ratingRow.league] || ratingRow.league,
    logo: getCachedLogo(name) || null,
    elo_rating: ratingRow.elo_rating,
    league_rank: rank,
    league_size: leagueRatings.length,
    recent_form_ppg: formPpg,
    recent_matches: recentMatches,
    next_fixture: nextFixture || null,
  });
});

// Head-to-head: every historical meeting between two teams, either venue.
router.get("/h2h", (req, res) => {
  const { home, away } = req.query;
  if (!home || !away) {
    return res.status(400).json({ error: "Both home and away query params are required" });
  }

  const meetings = db
    .prepare(
      `SELECT * FROM matches
       WHERE (home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?)
       ORDER BY date DESC`
    )
    .all(home, away, away, home);

  const summary = { home_wins: 0, away_wins: 0, draws: 0 };
  for (const m of meetings) {
    const homeSideWon = (m.home_team === home && m.result === "H") || (m.home_team === away && m.result === "A");
    const awaySideWon = (m.home_team === away && m.result === "H") || (m.home_team === home && m.result === "A");
    if (m.result === "D") summary.draws++;
    else if (homeSideWon) summary.home_wins++;
    else if (awaySideWon) summary.away_wins++;
  }

  res.json({ meetings, summary, total: meetings.length });
});

module.exports = router;
