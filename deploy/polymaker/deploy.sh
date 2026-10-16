#!/usr/bin/env bash
# Run poly-maker (Polymarket liquidity rewards) on the Ubuntu server in Docker.
#   deploy/polymaker/deploy.sh [host]          # default host: hp; remote dir ~/polymaker
#   deploy/polymaker/deploy.sh check [host]    # rewards + fills + open orders (is it profitable?)
# Source: ~/Desktop/Projects/vendor/poly-maker (config in its mycfg/). Only ONE engine per wallet: the laptop copy must
# be stopped first, or both would quote the same wallet.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${POLYMAKER_SRC:-$HOME/Desktop/Projects/vendor/poly-maker}"
if [ "${1:-}" = "check" ]; then
  ssh "${2:-hp}" 'docker exec polymaker uv run --no-sync python check.py'; exit 0
fi
HOST="${1:-hp}"
if pgrep -f "polymaker run" >/dev/null; then echo "a local polymaker engine is running: stop it first"; exit 1; fi
ssh "$HOST" 'mkdir -p ~/polymaker/app ~/polymaker/mycfg'
rsync -a --delete --exclude .venv --exclude .git --exclude mycfg --exclude .env --exclude '*.csv' --exclude state.db \
  "$SRC/" "$HOST:polymaker/app/"
rsync -a "$HERE/Dockerfile" "$HERE/docker-compose.yml" "$HERE/check.py" "$HOST:polymaker/"
# config only; the server keeps its own state.db, logs and journal
rsync -a "$SRC/mycfg/config.toml" "$SRC/mycfg/strategy.toml" "$SRC/mycfg/markets.toml" "$HOST:polymaker/mycfg/"
ssh "$HOST" 'umask 077; cat > ~/polymaker/.env' < "$SRC/.env"
ssh "$HOST" 'cd ~/polymaker && docker compose up -d --build && docker compose ps'
