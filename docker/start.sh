#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=== LucidLink Workspace Manager ==="
echo ""

# Folders from the app: share iCloud Drive, Desktop and Documents with the indexer (read-only)
# the first time, so they can be browsed and connected from Engine > Indexing.
if [ "$(uname)" = Darwin ] && [ -f .env ]; then
  for pair in "BLOCK_ROOT_ICLOUD=$HOME/Library/Mobile Documents/com~apple~CloudDocs" "BLOCK_ROOT_DESKTOP=$HOME/Desktop" "BLOCK_ROOT_DOCUMENTS=$HOME/Documents"; do
    key="${pair%%=*}"; dir="${pair#*=}"
    if ! grep -q "^$key=" .env && [ -d "$dir" ]; then
      printf '%s=%s\n' "$key" "$dir" >> .env
      echo "Shared $dir with the indexer (read-only) as $key."
    fi
  done
fi

# The engine computer's name labels its drives in the app ("AI-Factory-Mini · NAS").
# Set once from the computer's name; edit ENGINE_NAME in .env to rename.
if [ -f .env ] && ! grep -q '^ENGINE_NAME=' .env; then
  if [ "$(uname)" = Darwin ]; then name="$(scutil --get ComputerName 2>/dev/null || hostname -s)"; else name="$(hostname -s)"; fi
  printf 'ENGINE_NAME=%s\n' "$name" >> .env
  echo "Named this engine computer \"$name\" (ENGINE_NAME in .env)."
fi

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export GIT_SHA

echo "[1/3] Building file service and indexer containers (git ${GIT_SHA})..."
docker compose build --quiet lucidlink-fileservice lucidlink-indexer

echo "[2/3] Starting services..."
docker compose up -d

echo "[3/3] Waiting for health checks..."
for i in $(seq 1 30); do
  if docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
lines = sys.stdin.read().strip().split('\n')
services = [json.loads(l) for l in lines if l]
healthy = all('healthy' in s.get('Health','') for s in services)
print('healthy' if healthy else 'waiting')
sys.exit(0 if healthy else 1)
" 2>/dev/null; then
    break
  fi
  sleep 2
done

echo ""
echo "=== Status ==="
docker compose ps
echo ""
echo "Endpoints:"
echo "  Health:     http://localhost:8000/health"
echo "  Swagger:    http://localhost:8000/docs"
echo "  Files API:  http://localhost:8000/files"
echo "  Mgmt API:   http://localhost:8000/api/v1/filespaces"
echo "  Mgmt Direct: http://localhost:3003/api/v1/filespaces"
echo ""
echo "Test:"
echo "  curl http://localhost:8000/health"
echo "  curl -H 'Authorization: Bearer <token>' http://localhost:8000/filespaces"
