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
    echo "Current python version: $(python --version 2>&1)"
    echo "Install Python 3.9+ and recreate .venv:"
    echo "  rm -rf .venv && python -m venv .venv"
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
  python "setup_livekit.py"
fi

# Секреты хранятся только в livekit.env. Старые установки держали копии в livekit.yaml (keys:)
# и turnserver.conf (static-auth-secret) — переносим их в livekit.env без вопросов.
if grep -q '^keys:' "deploy/livekit/livekit.yaml" \
  || grep -q '^static-auth-secret=' "deploy/livekit/turnserver.conf" \
  || ! grep -q '^TURN_STATIC_AUTH_SECRET=.' "livekit.env"; then
  echo "Migrating secrets into livekit.env (single source of truth)..."
  require_supported_python
  python "setup_livekit.py" -y
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

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not accessible for $(id -un); using sudo docker"
  DOCKER=(sudo docker)
fi

# Секреты для LiveKit (LIVEKIT_KEYS) и coturn подставляются из livekit.env.
# up -d пересоздаёт контейнеры, если секреты в livekit.env изменились.
COMPOSE=("${DOCKER[@]}" compose --env-file "livekit.env" -f "deploy/livekit/docker-compose.yml")

echo "Starting LiveKit stack with Docker Compose..."
"${COMPOSE[@]}" up -d

# Правки примонтированного livekit.yaml (порты, TURN) LiveKit видит только после перезапуска процесса.
echo "Restarting livekit container to reload /etc/livekit/livekit.yaml..."
"${COMPOSE[@]}" restart livekit
sleep 2

HP="${HELPER_PORT:-8000}"
HH="${HELPER_HOST:-0.0.0.0}"
echo ""
echo "------------------------------------------------------------------------"
echo "  После строки 'Running on' ниже — блок «Параметры для GUI-клиента»."
echo "  Один IP/хост: задайте LIVEKIT_URL и HELPER_URL в livekit.env с этим адресом."
echo "  Пример: LIVEKIT_URL=ws://194.0.194.14:7880  HELPER_URL=http://194.0.194.14:${HP}"
echo "------------------------------------------------------------------------"
echo "Starting helper server on ${HH}:${HP}..."
exec python "server.py" --host "${HH}" --port "${HP}"
