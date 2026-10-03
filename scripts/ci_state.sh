#!/usr/bin/env bash
# State that has to survive between scheduled runs, kept as assets on a GitHub release tagged
# "state". A CI runner starts from an empty disk every time, but the SQLite file holds the
# accumulated track record (every prediction ever made and how it turned out) - losing it
# would reset that history. Release assets are overwritten in place, so unlike committing
# the database to a branch they never grow the repository.
#
#   predictions.db.gz   the SQLite database
#   xg.tar.gz           cached xG data (scraped, so worth not re-fetching)
#
# usage: scripts/ci_state.sh pack      build the two files into $STATE_DIR (default data/state)
#        scripts/ci_state.sh restore   download them (if the release exists) and unpack into data/
#        scripts/ci_state.sh save      pack, then upload (creating the release the first time)
#
# `restore` and `save` use the GitHub CLI (preinstalled on GitHub's runners; GH_TOKEN must be set).
set -euo pipefail

TAG=state
STATE_DIR="${STATE_DIR:-data/state}"

pack() {
  if [ ! -f data/predictions.db ]; then
    echo "No data/predictions.db to pack." >&2
    return 1
  fi
  mkdir -p "$STATE_DIR"
  gzip -c data/predictions.db > "$STATE_DIR/predictions.db.gz"
  if [ -d data/xg ]; then
    tar -czf "$STATE_DIR/xg.tar.gz" -C data xg
  fi
  ls -la "$STATE_DIR"
}

restore() {
  mkdir -p data "$STATE_DIR"
  if ! gh release view "$TAG" >/dev/null 2>&1; then
    echo "No '$TAG' release yet - starting with an empty database (the first run creates it)."
    return 0
  fi
  gh release download "$TAG" --dir "$STATE_DIR" --clobber
  if [ -f "$STATE_DIR/predictions.db.gz" ]; then
    gunzip -c "$STATE_DIR/predictions.db.gz" > data/predictions.db
    echo "Restored data/predictions.db ($(du -h data/predictions.db | cut -f1))"
  fi
  if [ -f "$STATE_DIR/xg.tar.gz" ]; then
    tar -xzf "$STATE_DIR/xg.tar.gz" -C data
    echo "Restored data/xg"
  fi
}

save() {
  pack
  if ! gh release view "$TAG" >/dev/null 2>&1; then
    gh release create "$TAG" --latest=false \
      --title "Prediction state (kept up to date by the refresh workflow - do not delete)" \
      --notes "The SQLite database and cached xG data the scheduled refresh restores at the start of every run."
  fi
  gh release upload "$TAG" "$STATE_DIR"/predictions.db.gz --clobber
  if [ -f "$STATE_DIR/xg.tar.gz" ]; then
    gh release upload "$TAG" "$STATE_DIR"/xg.tar.gz --clobber
  fi
}

case "${1:-}" in
  pack) pack ;;
  restore) restore ;;
  save) save ;;
  *) echo "usage: $0 pack|restore|save" >&2; exit 2 ;;
esac
