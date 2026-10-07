#!/usr/bin/env bash
# Deploy-on-merge for fern. Runs on the host (outside the sandbox), every 5 min via launchd (Mac) or systemd (Linux).
# Pulls main; if new, rebuilds, health-checks, and rolls back to the last good commit on failure.
# Usage: deploy/deployer.sh   (run from a dedicated clone, e.g. ~/fern-deploy, never your dev checkout)
set -euo pipefail
cd "$(dirname "$0")/.."
STATE=deploy/.deployed           # last good sha
BAD=deploy/.bad                  # sha that failed (skipped until main moves)
PORT=$(grep -E '^FERN_PORT=' deploy/.env 2>/dev/null | cut -d= -f2 || true); PORT=${PORT:-8766}
TOPIC=$(grep -E '^FERN_NTFY_TOPIC=' deploy/.env 2>/dev/null | cut -d= -f2 || true)

notify() { [ -n "$TOPIC" ] && curl -fsS -m 10 -H "Title: $1" -d "$2" "https://ntfy.sh/$TOPIC" >/dev/null || true; }
up() { (cd deploy && docker compose up -d --build --remove-orphans >/dev/null 2>&1); }
healthy() {
  for _ in $(seq 30); do
    curl -fsS -m 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

git fetch -q origin main
NEW=$(git rev-parse origin/main)
OLD=$(cat "$STATE" 2>/dev/null || echo none)  # none = first deploy on this host
[ "$NEW" = "$OLD" ] && exit 0
[ "$NEW" = "$(cat "$BAD" 2>/dev/null || true)" ] && exit 0

git checkout -q --detach "$NEW"
if up && healthy; then
  echo "$NEW" > "$STATE"
  notify "fern deployed" "$(git log -1 --format='%h %s' "$NEW")"
else
  echo "$NEW" > "$BAD"
  if [ "$OLD" = none ]; then
    msg="first deploy failed, nothing to roll back to"
  else
    git checkout -q --detach "$OLD"
    up && healthy && msg="rolled back to ${OLD:0:7}" || msg="ROLLBACK ALSO FAILED - check the host"
  fi
  notify "fern deploy failed" "$(git log -1 --format='%h %s' "$NEW"): $msg"
  exit 1
fi
