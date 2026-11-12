#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "\${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PROFILE="\${AVRONE_LOCAL_SANDBOX_PROFILE:-$ROOT/docs/local-ollama/local-ollama-sandbox.env}"
if [[ ! -f "$PROFILE" ]]; then
  echo "Missing local sandbox profile: $PROFILE" >&2
  echo "Copy docs/local-ollama/local-ollama-sandbox.env.example to that path, then edit AVRONE_LLM_MODEL if needed." >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "$PROFILE"
set +a
: "\${AVRONE_PROVIDER:=ollama}"
: "\${AVRONE_LLM_BASE_URL:=http://127.0.0.1:11434/v1}"
: "\${AVRONE_LLM_MODEL:=qwen2.5:3b}"
: "\${AVRONE_RUNNER:=docker}"
: "\${AVRONE_DOCKER_IMAGE:=python:3.12-slim}"
: "\${AVRONE_ALLOW_NETWORK:=false}"
: "\${AVRONE_AGENT_FS:=1}"
: "\${AVRONE_AGENT_CONTAINER:=persistent}"
: "\${AVRONE_AGENT_DOCKER_ISOLATE:=0}"
: "\${AVRONE_HOST:=127.0.0.1}"
: "\${AVRONE_PORT:=8710}"
export AVRONE_PROVIDER AVRONE_LLM_BASE_URL AVRONE_LLM_MODEL AVRONE_RUNNER
export AVRONE_DOCKER_IMAGE AVRONE_ALLOW_NETWORK AVRONE_AGENT_FS AVRONE_AGENT_CONTAINER
export AVRONE_AGENT_DOCKER_ISOLATE AVRONE_HOST AVRONE_PORT AVRONE_LLM_MODE
export AVRONE_AGENT_UID="\${AVRONE_AGENT_UID:-$(id -u)}"
export AVRONE_AGENT_GID="\${AVRONE_AGENT_GID:-$(id -g)}"
export NO_PROXY="127.0.0.1,localhost,::1\${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export PYTHONUNBUFFERED=1
if [[ "$(id -u)" == "0" ]]; then
  echo "Refusing to run the AVRONE host process as root. Use your normal macOS/Linux user." >&2
  exit 2
fi
if [[ "$AVRONE_AGENT_UID" != "$(id -u)" || "$AVRONE_AGENT_GID" != "$(id -g)" ]]; then
  echo "AVRONE_AGENT_UID/GID must match the owner of workspace/agents (normally id -u / id -g)." >&2
  echo "Expected $(id -u):$(id -g), got $AVRONE_AGENT_UID:$AVRONE_AGENT_GID." >&2
  exit 2
fi
if [[ "$AVRONE_PROVIDER" != "ollama" || "$AVRONE_RUNNER" != "docker" ||
      "$AVRONE_ALLOW_NETWORK" != "false" || "$AVRONE_AGENT_FS" != "1" ||
      "$AVRONE_AGENT_CONTAINER" != "persistent" ]]; then
  echo "Unsafe/incomplete profile. Required: provider=ollama, runner=docker, allow_network=false," >&2
  echo "agent_fs=1, agent_container=persistent." >&2
  exit 2
fi
command -v docker >/dev/null || { echo "Docker CLI not found; install/start Docker Desktop first." >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "Docker daemon unavailable; start Docker Desktop and retry." >&2; exit 2; }
command -v ollama >/dev/null || { echo "Ollama CLI not found; install Ollama first." >&2; exit 2; }
OLLAMA_HOST="\${OLLAMA_HOST:-http://127.0.0.1:11434}"
curl --noproxy '*' -fsS --max-time 8 "$OLLAMA_HOST/api/tags" >/dev/null ||
  { echo "Ollama API unavailable at $OLLAMA_HOST; start Ollama and retry." >&2; exit 2; }
curl --noproxy '*' -fsS --max-time 8 "\${AVRONE_LLM_BASE_URL%/}/models" >/dev/null ||
  { echo "Ollama OpenAI-compatible /v1/models endpoint unavailable at $AVRONE_LLM_BASE_URL." >&2; exit 2; }
ollama show "$AVRONE_LLM_MODEL" >/dev/null 2>&1 ||
  { echo "Model $AVRONE_LLM_MODEL is not installed. Run: ollama pull $AVRONE_LLM_MODEL" >&2; exit 2; }
if ! docker image inspect "$AVRONE_DOCKER_IMAGE" >/dev/null 2>&1; then
  echo "Pulling sandbox image $AVRONE_DOCKER_IMAGE ..."
  docker pull "$AVRONE_DOCKER_IMAGE"
fi
python3 -c "import avrone" >/dev/null 2>&1 ||
  { echo "AVRONE is not installed in this Python environment. Run: python3 -m pip install -e '.[dev]'." >&2; exit 2; }
if curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$AVRONE_PORT/api/status" >/dev/null 2>&1; then
  echo "An AVRONE server already responds on port $AVRONE_PORT. Stop it or choose another AVRONE_PORT." >&2
  exit 2
fi
echo "Starting AVRONE on loopback with Ollama=$AVRONE_LLM_MODEL and persistent, networkless Docker agent homes."
echo "In another terminal, run: python3 docs/local-ollama/test-local-ollama-squad.py"
exec python3 -m avrone.server
