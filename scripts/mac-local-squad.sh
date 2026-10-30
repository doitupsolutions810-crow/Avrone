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
DRIVE="$(cd "$DRIVE" && pwd -P)" || { echo "Could not resolve DRIVE"; exit 1; }
[ "$DRIVE" != "/" ] || { echo "Refusing to use the filesystem root as DRIVE"; exit 1; }

MODEL="${MODEL:-qwen3-coder-next:q4_K_M}"
LANES="${LANES:-deepcov newmods tools robust}"
LANE_TIMEOUT="${LANE_TIMEOUT:-7200}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
case "$LANE_TIMEOUT" in
  ''|*[!0-9]*) echo "LANE_TIMEOUT must be an integer from 60 to 43200 seconds"; exit 1 ;;
esac
[ "$LANE_TIMEOUT" -ge 60 ] && [ "$LANE_TIMEOUT" -le 43200 ] || {
  echo "LANE_TIMEOUT must be between 60 and 43200 seconds"; exit 1;
}
[ -n "$MODEL" ] || { echo "MODEL must not be empty"; exit 1; }
lane_count=0
seen_lanes=" "
for selected_lane in $LANES; do
  case "$selected_lane" in
    deepcov|newmods|tools|robust) ;;
    *) echo "Unknown lane: $selected_lane"; exit 1 ;;
  esac
  case "$seen_lanes" in
    *" $selected_lane "*) echo "Duplicate lane: $selected_lane"; exit 1 ;;
  esac
  seen_lanes="${seen_lanes}${selected_lane} "
  lane_count=$((lane_count + 1))
done
[ "$lane_count" -gt 0 ] || { echo "Select at least one lane in LANES"; exit 1; }
OUT="$DRIVE/avrone-runs/round5-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$DRIVE/ollama-models" "$OUT"
export OLLAMA_MODELS="$DRIVE/ollama-models"

log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/squad.log"; }

# Use a real timeout command for every long-running install, baseline and agent lane.
if ! command -v timeout >/dev/null 2>&1; then
  command -v brew >/dev/null || { log "timeout is unavailable and Homebrew is missing"; exit 1; }
  # Bootstrap exception: GNU timeout is the tool being installed here.
  brew install coreutils >/dev/null 2>&1 || { log "could not install coreutils for bounded execution"; exit 1; }
  command -v gtimeout >/dev/null 2>&1 || { log "gtimeout was not installed"; exit 1; }
  timeout() { command gtimeout "$@"; }
fi

# 1. Ollama
if ! command -v ollama >/dev/null; then
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
  log "installing ollama"
  timeout 1800 brew install ollama || { log "Ollama installation failed"; exit 1; }
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
  log "pulling $MODEL (large download, one time)"
  if ! timeout "$LANE_TIMEOUT" ollama pull "$MODEL"; then
    log "model pull failed or exceeded LANE_TIMEOUT"
    exit 1
  fi
fi
log "warming $MODEL"
if ! timeout 900 ollama run "$MODEL" "say ready" </dev/null > "$OUT/model-warmup.log" 2>&1; then
  log "model warmup failed; see $OUT/model-warmup.log"
  tail -20 "$OUT/model-warmup.log"
  exit 1
fi

# 3. AVRONE install
cd "$REPO" || { log "could not enter repository root"; exit 1; }
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)' || {
  log "Python 3.11 or 3.12 is required"
  exit 1
}
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv || { log "could not create .venv"; exit 1; }
fi
[ -f .venv/bin/activate ] || { log ".venv is incomplete; refusing to use ambient Python"; exit 1; }
source .venv/bin/activate || { log "could not activate .venv"; exit 1; }
if ! timeout 1800 pip install -q -e ".[dev]" > "$OUT/install.log" 2>&1; then
  log "editable install failed; tail:"
  tail -25 "$OUT/install.log"
  exit 1
fi

# 4. Check the tests pass before any lane runs
if [ "${SKIP_GATE:-0}" != "1" ]; then
  log "running baseline gate"
  if ! timeout "$LANE_TIMEOUT" python -m pytest -q -m "not integration" -x > "$OUT/baseline.txt" 2>&1; then
    log "baseline RED - fix main before evolving. tail:"; tail -15 "$OUT/baseline.txt"; exit 1
  fi
  log "baseline: $(tail -1 "$OUT/baseline.txt")"
fi

# Snapshot the beginning of this squad so persisted proposals from earlier
# runs cannot satisfy this run's per-lane acceptance threshold.
RUN_STARTED_EPOCH="$(python -c 'import time; print(repr(time.time()))')" || {
  log "could not establish a squad start timestamp"
  exit 1
}

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
  local rc=$?
  if [ "$rc" -eq 0 ] && [ -s "$OUT/$1.json" ]; then
    log "lane $1 completed rc=0"
    return 0
  fi
  log "lane $1 failed rc=$rc; inspect $OUT/$1.err and $OUT/$1.txt"
  return 1
}
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

# Read accepted proposals from each lane's own data directory. The CLI emits
# JSON for --json; require at least two all-green proposals per selected lane.
proposal_status=0
for selected_lane in $LANES; do
  lane_data="$DRIVE/avrone-data/$selected_lane"
  proposal_report="$OUT/$selected_lane.accepted.json"
  proposal_tmp="$proposal_report.tmp"
  log "checking accepted proposals for lane $selected_lane"
  if AVRONE_DATA_DIR="$lane_data" python -m avrone.cli evolve --json "$proposal_report" > "$proposal_tmp" 2> "$OUT/$selected_lane.evolve.err"; then
    if mv "$proposal_tmp" "$proposal_report"; then
      accepted_count=$(python -c 'import json,sys; rows=json.load(open(sys.argv[1])); started=float(sys.argv[2]); fresh=[r for r in rows if isinstance(r.get("ts"), (int,float)) and not isinstance(r.get("ts"), bool) and r["ts"] >= started]; print(len(fresh))' "$proposal_report" "$RUN_STARTED_EPOCH" 2>/dev/null)
      case "$accepted_count" in
        ''|*[!0-9]*) log "lane $selected_lane proposal report is invalid"; proposal_status=1 ;;
        *)
          log "lane $selected_lane accepted proposals=$accepted_count"
          if [ "$accepted_count" -lt 2 ]; then
            log "lane $selected_lane did not meet the two-accepted-change requirement"
            proposal_status=1
          fi
          ;;
      esac
    else
      log "could not save lane $selected_lane proposal report"
      proposal_status=1
    fi
  else
    log "could not list accepted proposals for lane $selected_lane; see $OUT/$selected_lane.evolve.err"
    proposal_status=1
  fi
done
if [ "$proposal_status" -ne 0 ]; then
  log "squad did not meet the acceptance threshold; review lane reports before integrating"
  exit 1
fi
log "all selected lanes have at least two accepted proposals"
log "results in $OUT  -  review with: python -m avrone.cli evolve --json <path>"
