#!/usr/bin/env bash
# GitHub switches off scheduled workflows in a PUBLIC repository after 60 days with no repository
# activity - the site would just stop updating, with no error anywhere. A commit counts as
# activity, so when the newest commit is MAX_AGE_DAYS old (default 30), add an empty one.
# Runs at the start of every refresh, so it fires roughly once a month and costs nothing the rest
# of the time. The identity is passed per-command: nothing in git's config is changed.
set -euo pipefail

MAX_AGE_DAYS="${MAX_AGE_DAYS:-30}"
BRANCH="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"

age_days=$(( ( $(date +%s) - $(git log -1 --format=%ct) ) / 86400 ))
if [ "$age_days" -lt "$MAX_AGE_DAYS" ]; then
  echo "Newest commit is $age_days day(s) old - the schedule is in no danger."
  exit 0
fi

git -c user.name="github-actions[bot]" \
    -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    commit --allow-empty -m "Keep the scheduled refresh alive (no commits for $age_days days)"
git push origin "HEAD:$BRANCH"
echo "Pushed a keep-alive commit to $BRANCH."
