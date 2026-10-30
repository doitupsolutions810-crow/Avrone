#!/usr/bin/env bash
# One-command AVRONE local squad for Apple Silicon Macs.
# Installs Ollama (if missing), stores models + data on an external drive,
# pulls a local coder model, installs AVRONE, checks that the tests pass,
# then runs the 4 round-5 self-improvement lanes on the local model. No cloud API needed.
#
# Usage:  DRIVE=/Volumes/AI ./scripts/mac-local-squad.sh
# Env:    MODEL (default qwen3-coder-next:q4_K_M), LANES, LANE_TIMEOUT (sec), SKIP_GATE=1
set -uo pipefail

DRIVE="${DRIVE:-}"
if [ -z "$DRIVE" ]; then
  echo "Available volumes:"; ls /Volumes
  echo "Set DRIVE, e.g.  DRIVE=/Volumes/AI $0"; exit 1
fi
[ -d "$DRIVE" ] || { echo "Drive $DRIVE not mounted"; exit 1; }
[ -w "$DRIVE" ] || { echo "Drive $DRIVE is not writable"; exit 1; }

MODEL="${MODEL:-qwen3-coder-next:q4_K_M}"
LANES="${LANES:-deepcov newmods tools robust}"
LANE_TIMEOUT="${LANE_TIMEOUT:-7200}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$DRIVE/avrone-runs/round5-$(date +%Y%m%d-%H%M)"
mkdir -p "$DRIVE/ollama-models" "$OUT"
export OLLAMA_MODELS="$DRIVE/ollama-models"

log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/squad.log"; }

# 1. Ollama
if ! command -v ollama >/dev/null; then
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
  log "installing ollama"; brew install ollama
fi
launchctl setenv OLLAMA_MODELS "$OLLAMA_MODELS" 2>/dev/null || true
if ! curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null; then
  log "starting ollama serve (models on $OLLAMA_MODELS)"
  nohup ollama serve > "$DRIVE/ollama.log" 2>&1 &
  for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi
curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null || { log "ollama did not start; see $DRIVE/ollama.log"; exit 1; }

# 2. Model
if ! ollama list | awk '{print $1}' | grep -qx "$MODEL"; then
  log "pulling $MODEL (large download, one time)"; ollama pull "$MODEL" || exit 1
fi
log "warming $MODEL"; ollama run "$MODEL" "say ready" </dev/null | head -1 | tee -a "$OUT/squad.log"

# 3. AVRONE install
cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
if ! pip install -q -e ".[dev]" > "$OUT/install.log" 2>&1; then
  log "editable install failed; tail:"
  tail -25 "$OUT/install.log"
  exit 1
fi

# 4. Check the tests pass before any lane runs
if [ "${SKIP_GATE:-0}" != "1" ]; then
  log "running baseline gate"
  if ! python -m pytest -q -m "not integration" -x > "$OUT/baseline.txt" 2>&1; then
    log "baseline RED - fix main before evolving. tail:"; tail -15 "$OUT/baseline.txt"; exit 1
  fi
  log "baseline: $(tail -1 "$OUT/baseline.txt")"
fi

# 5. Lanes
export AVRONE_PROVIDER=ollama AVRONE_LLM_BASE_URL=http://127.0.0.1:11434/v1
export AVRONE_LLM_MODEL="$MODEL" AVRONE_LEAD_MODEL="$MODEL" AVRONE_LLM_FALLBACKS=""
export AVRONE_RUNNER=local AVRONE_SELF_CHANGES=allow AVRONE_SELF_EVOLVE=allow
export AVRONE_WORKSPACE="$REPO" AVRONE_REPO_DIR="$REPO" AVRONE_SKILLS_DIR="$REPO/skills" AVRONE_PLUGINS_DIR="$REPO/plugins"
export AVRONE_MAX_STEPS=96 AVRONE_MAX_PARALLEL=2 AVRONE_LLM_TIMEOUT=900 NO_PROXY=127.0.0.1,localhost
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-4}"

COMMON='You are one lane of an AVRONE self-improvement squad (round 5). Your read/list/search tools share the repo root. Rules: ONLY create NEW files, small and genuinely useful, stdlib or installed deps, offline tests only (patch network). FIRST list tests/ and read the target module to find a genuinely uncovered branch; do not reuse an existing test file name. Submit each change with evolve_propose {goal, edits:[{path, content}]}; the harness runs the full non-integration suite in an isolated clone and keeps it only if ALL tests pass. If REJECTED, read the test tail and fix YOUR file, then resubmit. Never invent file paths: only use paths you have seen in a listing. Emit every tool use as a single JSON object {"tool":...,"args":...} and nothing else. Finish only after evolve_propose returned ACCEPTED twice, then call evolve_list and report each id + status. Your lane: '

lane(){
  case " $LANES " in *" $1 "*) ;; *) return;; esac
  local d="$DRIVE/avrone-data/$1"; mkdir -p "$d"
  log "lane $1 start"
  AVRONE_DATA_DIR="$d" timeout "$LANE_TIMEOUT" python -m avrone.cli chat "$COMMON$2" --json "$OUT/$1.json" > "$OUT/$1.txt" 2> "$OUT/$1.err"
  log "lane $1 rc=$?"
}
command -v timeout >/dev/null || { brew install coreutils >/dev/null 2>&1; alias timeout=gtimeout; command -v timeout >/dev/null || timeout(){ gtimeout "$@"; }; }

lane deepcov "DEEP COVERAGE: avrone/trinity.py, avrone/evolve.py, avrone/gateway.py uncovered error/edge branches. New files like tests/test_trinity_deep_r5.py. Land TWO accepted." &
pids=("$!")
lane newmods "COVERAGE for newest modules: avrone/governor.py, greatscale.py, checkpoint.py, memstore.py, agentfs.py, fsunify.py, bridge.py. New files tests/test_*_r5.py. Land TWO accepted." &
pids+=("$!")
lane tools "NEW TOOLS: read plugins/calculator.py; add plugins/agent_memory.py and plugins/hash_tool.py, each with its own focused test file. Land TWO accepted." &
pids+=("$!")
lane robust "ROBUSTNESS: avrone/recon.py (default-deny allowlist), osint.py, fleet.py, jobqueue.py edge cases offline. Land TWO accepted." &
pids+=("$!")

lane_status=0
for pid in "${pids[@]}"; do
  if wait "$pid"; then
    :
  else
    lane_status=1
  fi
done
if [ "$lane_status" -ne 0 ]; then
  log "one or more lanes failed or timed out; inspect each lane .err/.txt before review"
  exit "$lane_status"
fi

log "lanes finished. Accepted proposals:"
python -m avrone.cli evolve list 2>/dev/null | tee -a "$OUT/squad.log" || true
log "results in $OUT  -  review with: python -m avrone.cli evolve diff <id>"
