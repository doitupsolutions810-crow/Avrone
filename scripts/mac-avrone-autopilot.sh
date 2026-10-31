#!/usr/bin/env bash
# AVRONE autopilot for Apple Silicon Macs — finds the system, configures it and its bridges,
# installs a big free local model, and resumes the work exactly where it left off:
#   Track A: tool reintroduction (stage N of 9 in scripts/avrone-state/)
#   Track B: self-evolution squad lanes (accepted changes -> evolve/* branches, never main)
# Read scripts/AVRONE-HANDOFF.md first. No cloud, no API keys, no cost.
#
# Usage:  DRIVE=/Volumes/AI ./scripts/mac-avrone-autopilot.sh
# Env:    MODEL (default gpt-oss:120b)  CTX (default 32768)  ONCE=1  CYCLES=N
#         LANES ("deepcov newmods tools robust")  LANE_TIMEOUT (sec, 3600)  SMOKE_TIMEOUT (1800)  SKIP_GATE=1
set -uo pipefail

# ---------- 0. locate everything ----------
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$REPO/avrone/trinity.py" ] && [ -f "$REPO/pyproject.toml" ] || { echo "Not an AVRONE checkout: $REPO"; exit 1; }
DRIVE="${DRIVE:-}"
if [ -z "$DRIVE" ]; then echo "Volumes:"; ls /Volumes; echo "Set DRIVE, e.g. DRIVE=/Volumes/AI $0"; exit 1; fi
[ -d "$DRIVE" ] && [ -w "$DRIVE" ] || { echo "Drive $DRIVE missing or not writable"; exit 1; }

MODEL="${MODEL:-gpt-oss:120b}"; CTX="${CTX:-32768}"
LANES="${LANES:-deepcov newmods tools robust}"; LANE_TIMEOUT="${LANE_TIMEOUT:-3600}"
CYCLES="${CYCLES:-0}"; [ "${ONCE:-0}" = "1" ] && CYCLES=1
STATE_SRC="$REPO/scripts/avrone-state"; STATE="$DRIVE/avrone-state"
RUNS="$DRIVE/avrone-runs"; DATA="$DRIVE/avrone-data"
mkdir -p "$STATE" "$RUNS" "$DATA" "$DRIVE/ollama-models"
# persistent state lives on the drive; seed it from the repo copy the first time only
[ -f "$STATE/reintro-plan.json" ] || cp "$STATE_SRC/reintro-plan.json" "$STATE/"
[ -f "$STATE/reintro-stage.txt" ] || cp "$STATE_SRC/reintro-stage.txt" "$STATE/"
OUT="$RUNS/$(date +%Y%m%d-%H%M)"; mkdir -p "$OUT"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/autopilot.log"; }
log "AVRONE autopilot  repo=$REPO  drive=$DRIVE  model=$MODEL  stage=$(cat "$STATE/reintro-stage.txt")/9"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v timeout >/dev/null || { brew install coreutils >/dev/null 2>&1; }
TO=timeout; command -v timeout >/dev/null || TO=gtimeout

# ---------- 1. repo hygiene + carried patches ----------
cd "$REPO" || exit 1
git rev-parse --git-dir >/dev/null 2>&1 || { log "no git repo here"; exit 1; }
if [ -d agents ] && ! git ls-files --error-unmatch agents >/dev/null 2>&1 && ! git check-ignore -q agents; then
  log "removing untracked stray agents/ scratch dir"; rm -rf agents
fi
grep -q "def apply_tool_focus" avrone/bootstrap.py && grep -q "def coerce_args" avrone/trinity.py || {
  log "applying carried patches (TOOLS_ONLY, coerce_args)"
  git -c user.name=avrone -c user.email=avrone@local am "$STATE_SRC"/patches/*.patch \
    || { git am --abort 2>/dev/null; log "patch apply failed — resolve manually"; exit 1; }
}

# ---------- 2. LLM bridge: Ollama with big context ----------
command -v ollama >/dev/null || { log "installing ollama"; brew install ollama; }
export OLLAMA_MODELS="$DRIVE/ollama-models" OLLAMA_CONTEXT_LENGTH="$CTX" OLLAMA_KEEP_ALIVE=2h
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"
launchctl setenv OLLAMA_MODELS "$OLLAMA_MODELS" 2>/dev/null || true
launchctl setenv OLLAMA_CONTEXT_LENGTH "$CTX" 2>/dev/null || true
if ! curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null; then
  log "starting ollama serve (ctx=$CTX)"; nohup ollama serve > "$DRIVE/ollama.log" 2>&1 &
  for _ in $(seq 1 40); do curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi
curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null || { log "ollama did not start; see $DRIVE/ollama.log"; exit 1; }
if ! ollama list | awk 'NR>1{print $1}' | grep -qx "$MODEL"; then
  log "pulling $MODEL (one-time, large)"; ollama pull "$MODEL" || exit 1
fi
log "warming $MODEL: $(ollama run "$MODEL" 'reply with the single word ready' </dev/null 2>/dev/null | tr -d '\n' | head -c 60)"

# ---------- 3. install + baseline gate ----------
# AVRONE needs Python >= 3.11 (macOS ships 3.9): pick a new-enough interpreter or install one
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' && { PY="$c"; break; }
done
[ -n "$PY" ] || { log "installing python@3.12"; brew install python@3.12 && PY="$(brew --prefix)/bin/python3.12"; }
[ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null && { log "rebuilding old .venv"; rm -rf .venv; }
[ -d .venv ] || "$PY" -m venv .venv || { rm -rf .venv; log "venv creation failed for $PY"; exit 1; }
[ -f .venv/bin/activate ] || { rm -rf .venv; log "broken .venv (no activate) — rerun"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -e ".[dev]" || { log "pip install failed"; exit 1; }
if [ "${SKIP_GATE:-0}" != "1" ]; then
  log "baseline gate (non-integration suite)"
  if ! python -m pytest -q -p no:cacheprovider -m "not integration" > "$OUT/baseline.txt" 2>&1; then
    log "baseline RED — fix main before evolving:"; tail -20 "$OUT/baseline.txt"; exit 1
  fi
  log "baseline: $(tail -1 "$OUT/baseline.txt")"
fi

# ---------- 4. coherent env for every bridge ----------
cat > "$STATE/env.sh" <<EOF
export AVRONE_PROVIDER=ollama AVRONE_LLM_BASE_URL=http://127.0.0.1:11434/v1
export AVRONE_LLM_MODEL="$MODEL" AVRONE_LEAD_MODEL="$MODEL" AVRONE_LLM_FALLBACKS=""
export AVRONE_LLM_TIMEOUT=900 AVRONE_LLM_RETRIES=2
export AVRONE_RUNNER=local AVRONE_SELF_CHANGES=allow AVRONE_SELF_EVOLVE=allow
export AVRONE_WORKSPACE="$REPO" AVRONE_REPO_DIR="$REPO" AVRONE_SKILLS_DIR="$REPO/skills" AVRONE_PLUGINS_DIR="$REPO/plugins"
export AVRONE_MCP_CONFIG="$STATE/mcp.json"
export AVRONE_RECON_ALLOWLIST=127.0.0.1,localhost
export AVRONE_BRIDGE_MAX_POSTS=1000 AVRONE_MAX_PARALLEL=2
export NO_PROXY=127.0.0.1,localhost
export AVRONE_EVOLVE_TEST_CMD="python -m pytest -q -x -p no:cacheprovider -m 'not integration' tests"
EOF
[ -f "$STATE/mcp.json" ] || echo '{"servers":{}}' > "$STATE/mcp.json"
source "$STATE/env.sh"
log "bridges: provider=ollama ($MODEL)  learning-bridge=bridge.py  fleet=local  codex=on  recon=localhost-only"
AVRONE_DATA_DIR="$DATA/doctor" python -m avrone.cli doctor > "$OUT/doctor.txt" 2>&1 || log "doctor reported warnings (see doctor.txt)"
log "doctor: $(tail -1 "$OUT/doctor.txt")"

# ---------- helpers ----------
tools_for(){ python3 - "$STATE/reintro-plan.json" "$1" <<'PY'
import json,sys;p=json.load(open(sys.argv[1]));s=int(sys.argv[2]);B=p['batches']
cur=p['core']+[t for b in B[:s] for t in b]; nxt=B[s] if s<len(B) else []
print(','.join(cur)+' '+(','.join(nxt) or '-'))
PY
}

reintro_step(){   # Track A: smoke-test the next batch; advance when coherent
  local S; S=$(cat "$STATE/reintro-stage.txt")
  read CUR NEXT < <(tools_for "$S")
  [ "$NEXT" = "-" ] && { log "reintro complete: 9/9, all 75 tools active"; return 0; }
  log "reintro stage $S/9 — smoke-testing: $NEXT"
  local P="Tool check. Reply ONLY with one JSON object {\"tool\":NAME,\"args\":{...}} per turn. Call each of these tools exactly once with small, safe, valid arguments (read each tool's args doc; use path '.', text 'hello', host 127.0.0.1 where needed): $NEXT. Guardrails: port_scan only 127.0.0.1 with authorized=true and <=5 ports; shell/priv_shell/priv_admin only read-only commands (id, uname -a, df -h); fs_delete only a file you created; docker_run/env_install only if available (an 'unavailable' error counts as exercised); bridge_post then bridge_read then bridge_reply on the same post; spawn one tiny sub-agent with a one-line task. Finish with one line per tool: NAME OK or FAIL."
  AVRONE_TOOLS_ONLY="$CUR,$NEXT" AVRONE_MAX_STEPS=$(( $(echo "$NEXT" | tr , '\n' | wc -l) * 2 + 6 )) \
  AVRONE_DATA_DIR="$DATA/smoke" $TO "${SMOKE_TIMEOUT:-1800}" python -m avrone.cli chat "$P" > "$OUT/smoke-$S.txt" 2> "$OUT/smoke-$S.err"
  python3 - "$OUT/smoke-$S.err" "$NEXT" "$STATE/reintro-stage.txt" "$S" <<'PY' | tee -a "$OUT/autopilot.log"
import sys,re
log=open(sys.argv[1]).read(); nxt=sys.argv[2].split(',')
called=set(re.findall(r'tool\.call\s+\S+\s+(\S+)',log))
ok=[t for t in nxt if t in called]
bad=log.count('action.malformed')+len(re.findall(r'unknown tool',log,re.I))
passed=len(ok)/len(nxt)>=0.6 and bad<=3
print(f"  smoke: {len(ok)}/{len(nxt)} exercised, malformed/unknown={bad} -> {'ADVANCE' if passed else 'HOLD'}; missed={sorted(set(nxt)-set(ok))}")
if passed: open(sys.argv[3],'w').write(str(int(sys.argv[4])+1))
PY
}

COMMON='You are one lane of the AVRONE self-improvement squad. Reply ONLY with one JSON object {"tool":NAME,"args":{...}} per turn. Start with evolve_list; if it already shows two ACCEPTED proposals for your lane goal, finish. Read only real paths you have seen in a listing (list_files/read_file; if paths are prefixed shared/ in listings, evolve_propose paths are repo-relative WITHOUT shared/). Rules: create NEW files only, small and genuinely useful, stdlib or installed deps, offline tests only (patch network). Never write repo files with write_file/edit_file — put them only in evolve_propose {"goal":..., "edits":[{"path":..., "content":FULL FILE}]}. The harness runs the full non-integration suite in an isolated clone and keeps it only if ALL tests pass. If REJECTED, read the test tail, fix YOUR file, resubmit. You may run python/shell to test locally first, and post findings to the learning bridge (bridge_post) so other lanes see them. Finish after two ACCEPTED, then call evolve_list and report each id + status. Your lane: '

lane(){   # Track B
  case " $LANES " in *" $1 "*) ;; *) return;; esac
  local d="$DATA/lane-$1"; mkdir -p "$d"
  read CUR _ < <(tools_for "$(cat "$STATE/reintro-stage.txt")")
  log "lane $1 start ($(echo "$CUR" | tr , '\n' | wc -l | tr -d ' ') tools)"
  AVRONE_TOOLS_ONLY="$CUR" AVRONE_MAX_STEPS=96 AVRONE_DATA_DIR="$d" \
    $TO "$LANE_TIMEOUT" python -m avrone.cli chat "$COMMON$2" --json "$OUT/$1.json" > "$OUT/$1.txt" 2> "$OUT/$1.err"
  log "lane $1 rc=$? calls=$(grep -c 'tool.call' "$OUT/$1.err") proposals=$(grep -c 'evolve_propose' "$OUT/$1.err") accepted=$(grep -ci 'ACCEPTED' "$OUT/$1.err")"
}

squad(){   # wait only on lane PIDs: a bare `wait` would also block on the backgrounded ollama serve
  local a b
  lane deepcov "DEEP COVERAGE: avrone/trinity.py, avrone/evolve.py, avrone/gateway.py uncovered error/edge branches. New files like tests/test_trinity_deep_r6.py. Land TWO accepted." & a=$!
  lane newmods "COVERAGE for newest modules: avrone/governor.py, greatscale.py, checkpoint.py, memstore.py, agentfs.py, bridge.py. New files tests/test_<module>_r6.py. Land TWO accepted." & b=$!
  wait "$a" "$b"
  lane tools "NEW TOOLS: read plugins/calculator.py for the register(registry, settings) shape; add plugins/hash_tool.py (hash_text {text, algo}) and plugins/agent_memory.py, each with its own focused test file. Land TWO accepted." & a=$!
  lane robust "ROBUSTNESS: avrone/recon.py (default-deny allowlist), osint.py, fleet.py, jobqueue.py edge cases, offline. Land TWO accepted." & b=$!
  wait "$a" "$b"
}

report(){
  { echo "# AVRONE — accepted proposals awaiting review"; echo; echo "Generated $(date). Review with: AVRONE_DATA_DIR=<lane data dir> python -m avrone.cli evolve diff <id>. Do NOT merge without Crow's go-ahead."; echo
    echo "Reintro stage: $(cat "$STATE/reintro-stage.txt")/9"; echo; echo '```'
    for d in "$DATA"/lane-*; do echo "## $d"; AVRONE_DATA_DIR="$d" python -m avrone.cli evolve 2>/dev/null; done; echo '```'; } > "$REPO/MERGE_ME.md"
  cp "$REPO/MERGE_ME.md" "$OUT/MERGE_ME.md"
  log "summary written: MERGE_ME.md (and $OUT)"
}

# ---------- 5. the loop ----------
n=0
while :; do
  n=$((n+1)); log "=== cycle $n ==="
  reintro_step
  squad
  report
  [ "$CYCLES" != "0" ] && [ "$n" -ge "$CYCLES" ] && break
  sleep 30
done
log "done. results in $OUT"
