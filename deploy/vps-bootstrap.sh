#!/usr/bin/env bash
# AVRONE VPS bootstrap - run as a NON-ROOT user with sudo, on a fresh Ubuntu 22.04/24.04.
# Loopback-only by default: AVRONE_HOST=127.0.0.1 + AVRONE_PUBLIC_ACCESS=loopback.
# Put AVRONE on a persistent volume BEFORE running this: set $AV_HOME (data dir) there.
set -euo pipefail

AV_REPO="${AV_REPO:-https://github.com/doitupsolutions810-crow/Avrone.git}"
AV_SRC="${AV_SRC:-$HOME/avrone}"
AV_HOME="${AV_HOME:-$HOME/avrone-data}"   # move to your persistent volume
AV_PORT="${AV_PORT:-8787}"

say() { printf '\n==> %s\n' "$*"; }

say "1/7 apt packages + docker (skip docker with SKIP_DOCKER=1)"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git curl openssl jq ca-certificates
if [ "${SKIP_DOCKER:-0}" != "1" ] && ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

say "2/7 clone + venv + editable install"
if [ ! -d "$AV_SRC/.git" ]; then git clone "$AV_REPO" "$AV_SRC"; fi
cd "$AV_SRC"
git fetch --tags
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]" pytest-cov websockets

say "3/7 run the full offline gate (source of truth: CI is billing-locked)"
python -m pytest -q

say "4/7 data dir on the persistent volume"
mkdir -p "$AV_HOME"
mkdir -p "$HOME/avrone-workspace"

say "5/7 generate tokens + .env.production (mode 600, loopback-only)"
if [ ! -f "$AV_SRC/.env.production" ]; then
  ADMIN_TOKEN="$(openssl rand -hex 32)"
  FLEET_TOKEN="$(openssl rand -hex 32)"
  umask 177
  cat > "$AV_SRC/.env.production" <<EOF
AVRONE_ENV_FILE=$AV_SRC/.env.production
AVRONE_DATA_DIR=$AV_HOME
AVRONE_WORKSPACE=$HOME/avrone-workspace
AVRONE_HOST=127.0.0.1
AVRONE_PORT=$AV_PORT
AVRONE_PUBLIC_ACCESS=loopback
AVRONE_ADMIN_TOKEN=$ADMIN_TOKEN
AVRONE_FLEET_TOKEN=$FLEET_TOKEN
AVRONE_LLM_MODE=mock
EOF
  chmod 600 "$AV_SRC/.env.production"
  say "tokens written to .env.production - move them to your secret store now"
else
  say ".env.production already exists - leaving it untouched"
fi

say "6/7 systemd unit (avrone.service)"
sudo tee /etc/systemd/system/avrone.service >/dev/null <<EOF
[Unit]
Description=AVRONE server
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$AV_SRC
Environment=AVRONE_ENV_FILE=$AV_SRC/.env.production
ExecStart=$AV_SRC/.venv/bin/python -m avrone.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now avrone
sleep 3
curl -fsS "http://127.0.0.1:$AV_PORT/api/status" && say "server is up on loopback" || say "server did not answer yet - check: journalctl -u avrone -n 50"

say "7/7 optional: local model (Ollama + qwen2.5:3b, small-context profile)"
if [ "${WITH_OLLAMA:-0}" = "1" ]; then
  if ! command -v ollama >/dev/null; then curl -fsSL https://ollama.com/install.sh | sh; fi
  sudo systemctl edit --no-pager ollama.service >/dev/null 2>&1 || true
  OLLAMA_CONTEXT_LENGTH=4096 ollama pull qwen2.5:3b
  say "start the live-model profile with:"
  say "  AVRONE_ENV_FILE=$AV_SRC/.env.production AVRONE_PROVIDER=ollama AVRONE_LLM_MODEL=qwen2.5:3b $AV_SRC/.venv/bin/python -m avrone.server"
  say "then benchmark: NO_PROXY=127.0.0.1 $AV_SRC/.venv/bin/python tools/model_bench.py"
fi

say "done. Next: follow deploy/VPS_RUNBOOK.md (exposure_check, hardening, backups)."
