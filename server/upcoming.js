// What "still to be played" means to every query that lists upcoming matches. A match with a
// kickoff time is over a few hours after it starts; an older row that only has a date is
// over once that date has passed. Finished matches aren't shown again anywhere - not even
// while predict.py is still waiting on a result to settle them.
//
// The cutoff here is deliberately generous (a long tennis match can run for hours); the page
// itself applies a tighter, per-sport cutoff on top of this, in the viewer's own clock.

const FINISHED_AFTER_HOURS = 6;

// Second precision, to compare cleanly against the "YYYY-MM-DDTHH:MM:SSZ" strings predict.py stores.
const iso = (date) => date.toISOString().replace(/\.\d{3}Z$/, "Z");

function upcomingFilter(now = new Date()) {
  const cutoff = new Date(now.getTime() - FINISHED_AFTER_HOURS * 3600 * 1000);
  return {
    sql: `actual_outcome IS NULL AND (
            (kickoff_utc IS NOT NULL AND kickoff_utc >= @cutoffTime)
            OR (kickoff_utc IS NULL AND match_date >= @cutoffDate)
          )`,
    params: { cutoffTime: iso(cutoff), cutoffDate: iso(cutoff).slice(0, 10) },
  };
}

module.exports = { upcomingFilter };
