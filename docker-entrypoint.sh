#!/bin/sh
# Container startup: make sure predictions.db and the website's JSON snapshots exist
# before the Express server starts serving them, then keep both fresh on a schedule
# in the background - the cloud equivalent of run_predict.ps1 + Windows Task Scheduler.
set -e

mkdir -p data

# predict.py exits non-zero if any one part (a sport, a feed) failed - but it still saves
# everything that worked, and the database exists from its first line, so a partial
# failure must not stop the website coming up.
if [ ! -f data/predictions.db ]; then
  echo "=== First run - building initial predictions: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> data/predict_log.txt
  python3 src/predict.py >> data/predict_log.txt 2>&1 || echo "First run reported failures - see above" >> data/predict_log.txt
fi

# The snapshots live in the image's filesystem, not the data volume, so a restart loses
# them - regenerating them from the (persistent) database takes a couple of seconds.
python3 src/site_export.py >> data/predict_log.txt 2>&1 || echo "Website data export failed - see above" >> data/predict_log.txt

# Two cadences, run one after the other (never at the same time - two writers on one
# SQLite file would fight over its lock):
#   - a LIGHT refresh (national teams + tennis + settling finished matches from ESPN) is
#     cheap, so it runs often: finished matches drop off the site, and new fixtures and
#     tennis draws/order-of-play appear, within about an hour
#   - a FULL refresh also re-fetches and refits the club-league models, which is slow
#     and changes slowly, so it runs on the longer interval
# Override either, in seconds, with service variables.
LIGHT_REFRESH_INTERVAL_SECONDS="${LIGHT_REFRESH_INTERVAL_SECONDS:-3600}"
REFRESH_INTERVAL_SECONDS="${REFRESH_INTERVAL_SECONDS:-21600}"

(
  since_full=0
  while true; do
    sleep "$LIGHT_REFRESH_INTERVAL_SECONDS"
    since_full=$((since_full + LIGHT_REFRESH_INTERVAL_SECONDS))
    if [ "$since_full" -ge "$REFRESH_INTERVAL_SECONDS" ]; then
      since_full=0
      echo "=== Scheduled full run: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> data/predict_log.txt
      python3 src/predict.py >> data/predict_log.txt 2>&1 || echo "Scheduled full run reported failures - see above" >> data/predict_log.txt
    else
      echo "=== Scheduled light run: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> data/predict_log.txt
      python3 src/predict.py --only intl tennis >> data/predict_log.txt 2>&1 || echo "Scheduled light run reported failures - see above" >> data/predict_log.txt
    fi
  done
) &

exec node server/server.js
