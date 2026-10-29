#!/usr/bin/env bash
# Deploy the drift paper trader to the Ubuntu server and (re)start it.
#   deploy/drift5d/deploy.sh [host]        # default host: hp (ssh alias); remote dir ~/drift5d
#   deploy/drift5d/deploy.sh pull [host]   # copy the server's dataset and logs to results/server/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [ "${1:-}" = "pull" ]; then
  HOST="${2:-hp}"; mkdir -p "$ROOT/results/server"
  rsync -az "$HOST:drift5d/data/" "$ROOT/results/server/"
  echo "pulled to results/server/"; exit 0
fi
HOST="${1:-hp}"
ssh "$HOST" 'mkdir -p ~/drift5d/scripts ~/drift5d/data'
rsync -a "$ROOT/deploy/drift5d/Dockerfile" "$ROOT/deploy/drift5d/docker-compose.yml" "$HOST:drift5d/"
rsync -a "$ROOT"/scripts/{build_alpaca_labels,drift5d_paper,jev_score,jev_drift}.py "$HOST:drift5d/scripts/"
# only the keys the trader needs (no Polymarket wallet keys on the server)
grep -E '^(ALPACA_API_KEY|ALPACA_SECRET_KEY|ALPACA_PAPER2_BASE_URL|ALPACA_PAPER2_API_KEY|ALPACA_PAPER2_SECRET_KEY|TYPESAFE_API_KEY)=' "$ROOT/.env" \
  | ssh "$HOST" 'umask 077; cat > ~/drift5d/.env'
ssh "$HOST" 'cd ~/drift5d && docker compose up -d --build && docker compose ps'
