#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if [[ ! -f "livekit.env" || ! -f "deploy/livekit/livekit.yaml" || ! -f "deploy/livekit/turnserver.conf" ]]; then
  echo "First run setup..."
  python3 "setup_livekit.py"
fi

if [[ -f "livekit.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "livekit.env"
  set +a
fi

if [[ ! -f "deploy/livekit/livekit.yaml" ]]; then
  echo "Missing deploy/livekit/livekit.yaml"
  echo "Create from template:"
  echo "  cp deploy/livekit/livekit.yaml.example deploy/livekit/livekit.yaml"
  exit 1
fi

if [[ ! -f "deploy/livekit/turnserver.conf" ]]; then
  echo "Missing deploy/livekit/turnserver.conf"
  echo "Create from template:"
  echo "  cp deploy/livekit/turnserver.conf.example deploy/livekit/turnserver.conf"
  exit 1
fi

echo "Starting LiveKit stack with Docker Compose..."
docker compose -f "deploy/livekit/docker-compose.yml" up -d

echo "Starting helper server on ${HELPER_HOST:-0.0.0.0}:${HELPER_PORT:-8000}..."
exec python3 "server.py" --host "${HELPER_HOST:-0.0.0.0}" --port "${HELPER_PORT:-8000}"
