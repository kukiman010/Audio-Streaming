#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

require_supported_python() {
  local major minor
  major="$(python -c 'import sys; print(sys.version_info[0])')"
  minor="$(python -c 'import sys; print(sys.version_info[1])')"

  if (( major < 3 || (major == 3 && minor < 9) )); then
    echo "Error: Python 3.9+ is required for the livekit package."
    echo "Current python3 version: $(python --version 2>&1)"
    echo "Install Python 3.9+ and recreate .venv:"
    echo "  rm -rf .venv && python3 -m venv .venv"
    exit 1
  fi
}

ensure_python_env() {
  if [[ ! -d ".venv" ]]; then
    echo "Creating Python virtualenv..."
    python -m venv .venv
  fi

  # shellcheck disable=SC1091
  source ".venv/bin/activate"

  if ! python -c "import livekit.api, aiohttp" >/dev/null 2>&1; then
    echo "Installing Python dependencies into .venv..."
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
  fi
}

if [[ ! -f "livekit.env" || ! -f "deploy/livekit/livekit.yaml" || ! -f "deploy/livekit/turnserver.conf" ]]; then
  echo "First run setup..."
  require_supported_python
  python3 "setup_livekit.py"
fi

require_supported_python
ensure_python_env

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
exec python "server.py" --host "${HELPER_HOST:-0.0.0.0}" --port "${HELPER_PORT:-8000}"
