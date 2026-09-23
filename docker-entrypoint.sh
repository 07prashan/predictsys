#!/bin/sh
# Container startup: make sure predictions.db exists before the Express server
# (which opens it with fileMustExist) ever starts, then keep it fresh on a
# schedule in the background - the cloud equivalent of run_predict.ps1 +
# Windows Task Scheduler.
set -e

mkdir -p data

if [ ! -f data/predictions.db ]; then
  echo "=== First run - building initial predictions: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> data/predict_log.txt
  python3 src/predict.py >> data/predict_log.txt 2>&1
fi

# How often to re-fetch results and re-predict, in seconds. Override by
# setting REFRESH_INTERVAL_SECONDS in Railway's service variables.
REFRESH_INTERVAL_SECONDS="${REFRESH_INTERVAL_SECONDS:-21600}"

(
  while true; do
    sleep "$REFRESH_INTERVAL_SECONDS"
    echo "=== Scheduled run: $(date -u +"%Y-%m-%d %H:%M:%S UTC") ===" >> data/predict_log.txt
    python3 src/predict.py >> data/predict_log.txt 2>&1 || echo "Scheduled run failed - see above" >> data/predict_log.txt
  done
) &

exec node server/server.js
