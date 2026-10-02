const express = require("express");
const db = require("../db");
const { logoFor } = require("../logo_lookup");
const { upcomingFilter } = require("../upcoming");
const { LEAGUE_NAMES, TENNIS_TOURS } = require("../leagues");

const router = express.Router();

function pointsFor(team, result, home) {
  if (result === "D") return 1;
  const teamWon = (home && result === "H") || (!home && result === "A");
  return teamWon ? 3 : 0;
}

// Team (or tennis player) profile: current rating + rank, recent form, next fixture.
// A name can in principle exist in two competitions, so the caller says which one
// (?league=) - the match card it was clicked from always knows.
router.get("/team/:name", (req, res) => {
  const name = req.params.name;
  const { league } = req.query;

  const ratingRow = league
    ? db.prepare("SELECT * FROM team_ratings WHERE team = ? AND league = ?").get(name, league)
    : db.prepare("SELECT * FROM team_ratings WHERE team = ?").get(name);
  if (!ratingRow) {
    return res.status(404).json({ error: `No data for "${name}"` });
  }
  const isTennis = TENNIS_TOURS.has(ratingRow.league);

  const leagueRatings = db
    .prepare("SELECT team, elo_rating FROM team_ratings WHERE league = ? ORDER BY elo_rating DESC")
    .all(ratingRow.league);
  const rank = leagueRatings.findIndex((r) => r.team === name) + 1;

  const recentMatches = db
    .prepare(
      `SELECT * FROM matches WHERE league = ? AND (home_team = ? OR away_team = ?) ORDER BY date DESC LIMIT 10`
    )
    .all(ratingRow.league, name, name)
    .map((m) => {
      const isHome = m.home_team === name;
      const opponent = isHome ? m.away_team : m.home_team;
      const goalsFor = isHome ? m.home_goals : m.away_goals;
      const goalsAgainst = isHome ? m.away_goals : m.home_goals;
      const points = pointsFor(name, m.result, isHome);
      return {
        date: m.date,
        opponent,
        opponent_logo: logoFor(opponent),
        venue: isHome ? "H" : "A",
        goals_for: goalsFor,
        goals_against: goalsAgainst,
        outcome: points === 3 ? "W" : points === 1 ? "D" : "L",
      };
    });

  const formPpg = recentMatches.length
    ? recentMatches.reduce((sum, m) => sum + ({ W: 3, D: 1, L: 0 }[m.outcome]), 0) / recentMatches.length
    : null;
  const winRate = recentMatches.length
    ? recentMatches.filter((m) => m.outcome === "W").length / recentMatches.length
    : null;

  const filter = upcomingFilter();
  const nextFixture = db
    .prepare(
      `SELECT league, match_date, kickoff_utc, home_team, away_team, prob_home, prob_draw, prob_away, predicted_outcome
       FROM predictions
       WHERE league = @league AND (home_team = @name OR away_team = @name) AND ${filter.sql}
       ORDER BY COALESCE(kickoff_utc, match_date) ASC LIMIT 1`
    )
    .get({ ...filter.params, league: ratingRow.league, name });

  res.json({
    team: name,
    sport: isTennis ? "tennis" : "football",
    league: ratingRow.league,
    league_name: LEAGUE_NAMES[ratingRow.league] || ratingRow.league,
    logo: logoFor(name),
    elo_rating: ratingRow.elo_rating,
    league_rank: rank,
    league_size: leagueRatings.length,
    recent_form_ppg: formPpg,
    recent_win_rate: winRate,
    recent_matches: recentMatches,
    next_fixture: nextFixture || null,
  });
});

// Head-to-head: every historical meeting between two teams/players, either venue.
router.get("/h2h", (req, res) => {
  const { home, away, league } = req.query;
  if (!home || !away) {
    return res.status(400).json({ error: "Both home and away query params are required" });
  }

  const meetings = db
    .prepare(
      `SELECT * FROM matches
       WHERE ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))
         AND (? IS NULL OR league = ?)
       ORDER BY date DESC`
    )
    .all(home, away, away, home, league ?? null, league ?? null);

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
