#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@120.79.167.211}"
REMOTE_DIR="${REMOTE_DIR:-/opt/weekly-push-tool}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.by56.yml}"

cd "$(dirname "$0")/.."

echo "==> Sync project to ${SERVER}:${REMOTE_DIR}"
rsync -az --delete --progress \
  --exclude ".git/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".env" \
  --exclude "data/" \
  --exclude "logs/" \
  ./ "${SERVER}:${REMOTE_DIR}/"

echo "==> Rebuild and restart Docker service"
ssh "${SERVER}" "cd '${REMOTE_DIR}' && docker compose -f '${COMPOSE_FILE}' up -d --build"

echo "==> Health check"
ssh "${SERVER}" "curl -fsS http://127.0.0.1:8010/api/health"

echo
echo "Deploy finished."
