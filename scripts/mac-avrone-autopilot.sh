#!/usr/bin/env bash
# started as `sh script` (dash/posix sh)? re-exec under bash before any bash-only syntax is parsed
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
# launched with stdout/stderr/stdin closed (launchd, cron, `>&-`)? point them at /dev/null so tools that
# print (ollama list, git) still work — a closed fd 1 made `ollama list` fail and triggered a needless re-pull
# (bash's `test` checks /dev/fd/N against the real descriptor; order matters — fix 0,1,2 before anything opens a pipe)
[ -e /dev/fd/0 ] || exec 0</dev/null
[ -e /dev/fd/1 ] || exec 1>/dev/null
[ -e /dev/fd/2 ] || exec 2>/dev/null
# AVRONE autopilot for Apple Silicon Macs — finds the system, configures it and its bridges,
# installs a big free local model, and resumes the work exactly where it left off:
#   Track A: tool reintroduction (stage N of 9 in scripts/avrone-state/)
#   Track B: self-evolution squad lanes (accepted changes -> evolve/* branches, never main)
# Read scripts/AVRONE-HANDOFF.md first. No cloud, no API keys, no cost.
#
# Usage:  DRIVE=/Volumes/AI ./scripts/mac-avrone-autopilot.sh
# Env:    MODEL (gpt-oss:120b)  CTX (32768)  ONCE=1  CYCLES=N (0 = forever)  CYCLE_SLEEP (30)
#         LANES ("deepcov newmods tools robust")  LANE_TIMEOUT (3600)  SMOKE_TIMEOUT (1800)
#         SKIP_GATE=1  GATE_CMD (override the baseline gate)  MIN_FREE_GB (80, checked before a pull)
#         OLLAMA_HOST (127.0.0.1:11434)  AUTOPILOT_NO_INSTALL=1 (skip pip; for tests)
# Exit:   0 done · 1 config/precondition error · 2 already running · 130/143 interrupted
set -uo pipefail

# ---------- 0. locate everything ----------
SELF="$0"   # follow symlinks (e.g. linked into /usr/local/bin) to find the real repo — readlink -f is not on old macOS
while [ -L "$SELF" ]; do _t="$(readlink "$SELF")"; case "$_t" in /*) SELF="$_t";; *) SELF="$(dirname "$SELF")/$_t";; esac; done
REPO="$(cd "$(dirname "$SELF")/.." && pwd)"
OUT=""
log(){ local m; m="[$(date +%H:%M:%S)] $*"; if [ -n "$OUT" ]; then echo "$m" | tee -a "$OUT/autopilot.log"; else echo "$m"; fi; }
die(){ log "FATAL: $*"; exit 1; }

{ [ -f "$REPO/avrone/trinity.py" ] && [ -f "$REPO/pyproject.toml" ]; } || die "not an AVRONE checkout: $REPO"
DRIVE="${DRIVE:-}"
if [ -z "$DRIVE" ]; then
  [ -d /Volumes ] && { echo "Volumes:"; ls /Volumes; }
  die "set DRIVE, e.g. DRIVE=/Volumes/AI $0"
fi
[ "$DRIVE" = / ] && die "DRIVE must not be the filesystem root"
DRIVE="${DRIVE%/}"
[ -d "$DRIVE" ] || die "drive $DRIVE is not mounted / does not exist"
DRIVE_REAL="$(cd -P "$DRIVE" && pwd)"; REPO_REAL="$(cd -P "$REPO" && pwd)"
[ "$DRIVE_REAL" = / ] && die "DRIVE must not be the filesystem root"
case "$DRIVE_REAL/" in "$REPO_REAL"/*) die "DRIVE must not be inside the repo ($REPO) — state would end up in git";; esac
[ -n "${HOME:-}" ] && [ "$DRIVE_REAL" = "$(cd -P "$HOME" 2>/dev/null && pwd)" ] && die "DRIVE must not be your home folder — point it at the external drive"
[ -w "$DRIVE" ] && touch "$DRIVE/.avrone-write-test" 2>/dev/null && rm -f "$DRIVE/.avrone-write-test" \
  || die "drive $DRIVE is not writable"

MODEL="${MODEL:-gpt-oss:120b}"; CTX="${CTX:-32768}"
LANES="${LANES-deepcov newmods tools robust}"; LANE_TIMEOUT="${LANE_TIMEOUT:-3600}"
# MODEL/LANES end up inside env.sh (sourced later) — allow only plain identifiers, never shell syntax
case "$MODEL" in ''|*[!A-Za-z0-9._:/+-]*) die "MODEL has illegal characters: '$MODEL'";; esac
for _l in ${LANES-}; do case "$_l" in deepcov|newmods|tools|robust) ;; *) die "unknown lane '$_l' (valid: deepcov newmods tools robust)";; esac; done
if [ "$(id -u)" = 0 ] && [ -z "${ALLOW_ROOT:-}" ]; then die "do not run as root/sudo — files on the drive would become root-owned (set ALLOW_ROOT=1 to force)"; fi
CYCLES="${CYCLES:-0}"; [ "${ONCE:-0}" = "1" ] && CYCLES=1
case "$CYCLES" in ''|*[!0-9]*) die "CYCLES must be a non-negative integer (got '$CYCLES')";; esac
case "$CTX" in ''|*[!0-9]*) die "CTX must be an integer (got '$CTX')";; esac
{ [ "${#CTX}" -le 7 ] && [ "$CTX" -ge 2048 ] && [ "$CTX" -le 1048576 ]; } || die "CTX must be between 2048 and 1048576 (got $CTX)"
OHOST="${OLLAMA_HOST:-127.0.0.1:11434}"; OHOST="${OHOST#http://}"; OHOST="${OHOST#https://}"; OHOST="${OHOST%%/*}"
case "$OHOST" in *:*) ;; '') OHOST=127.0.0.1:11434;; *) OHOST="$OHOST:11434";; esac
case "$OHOST" in *[!A-Za-z0-9.:_-]*) die "OLLAMA_HOST has illegal characters: '$OHOST'";; esac
export OLLAMA_HOST="$OHOST"
command -v git >/dev/null || die "git is required (xcode-select --install)"
command -v python3 >/dev/null || die "python3 is required"
OURL="http://$OHOST"
STATE_SRC="$REPO/scripts/avrone-state"; STATE="$DRIVE/avrone-state"
RUNS="$DRIVE/avrone-runs"; DATA="$DRIVE/avrone-data"
mkdir -p "$STATE" "$RUNS" "$DATA" "$DRIVE/ollama-models" || die "cannot create dirs on $DRIVE"
OUT="$RUNS/$(date +%Y%m%d-%H%M%S)-$$"; mkdir -p "$OUT" || die "cannot create $OUT"

# ---------- single-instance lock (mkdir is atomic; works on macOS without flock) ----------
LOCK="$STATE/.autopilot.lock"
KEEP_RUNS="${KEEP_RUNS:-30}"; case "$KEEP_RUNS" in ''|*[!0-9]*) die "KEEP_RUNS must be an integer";; esac
if ! mkdir "$LOCK" 2>/dev/null; then
  other="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    log "another autopilot (pid $other) is already running on $DRIVE — exiting"; exit 2
  fi
  log "removing stale lock (pid ${other:-unknown} is gone)"; rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { log "lost the lock race — exiting"; exit 2; }
fi
echo $$ > "$LOCK/pid"
DRIVE_TOKEN="$$-$RANDOM-$(date +%s)"; echo "$DRIVE_TOKEN" > "$LOCK/token"
check_drive(){   # on macOS an unplugged /Volumes/X must never be silently recreated on the internal disk
  [ "$(cat "$LOCK/token" 2>/dev/null)" = "$DRIVE_TOKEN" ] && return 0
  OUT=""; LOCK_GONE=1; die "drive $DRIVE disappeared or was swapped mid-run — replug it and rerun (state resumes)"
}

OLLAMA_PID=""; LANE_PIDS=""; SLEEP_PID=""
# `wait` returns early (>128) whenever a trapped signal (HUP, USR1) arrives; keep waiting while the child lives
wait_pid(){ local p="$1" rc rc2
  while :; do
    wait "$p"; rc=$?
    [ "$rc" -gt 128 ] || return "$rc"
    kill -0 "$p" 2>/dev/null && continue
    wait "$p" 2>/dev/null; rc2=$?; [ "$rc2" = 127 ] && return "$rc"; return "$rc2"
  done; }
bg_wait(){ "$@" & local p=$!; wait_pid "$p"; }   # TERM/INT act at once even during long steps
descendants(){ local c; for c in $(pgrep -P "$1" 2>/dev/null); do descendants "$c"; echo "$c"; done; }
cleanup(){
  local rc=$?
  for p in $LANE_PIDS $SLEEP_PID; do kill "$p" 2>/dev/null; done
  local tree; tree="$(descendants $$)"   # collect first, then kill: lanes -> timeout -> python -> pytest clones
  [ -n "$tree" ] && { kill $tree 2>/dev/null; sleep 1; kill -9 $tree 2>/dev/null; }
  [ -n "$OLLAMA_PID" ] && kill "$OLLAMA_PID" 2>/dev/null
  [ -z "${LOCK_GONE:-}" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK"
  [ -n "${RLOCK:-}" ] && [ "$(cat "$RLOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$RLOCK"
  exit "$rc"
}
trap cleanup EXIT
trap 'log "interrupted"; exit 130' INT
trap 'log "terminated"; exit 143' TERM
trap 'log "quit"; exit 131' QUIT
CYCLE_N=0; PHASE=startup
trap 'log "status: pid=$$ cycle=$CYCLE_N phase=$PHASE stage=$(read_stage 2>/dev/null)/${NB:-?} lanes=[${LANE_PIDS:-none}] out=$OUT"' USR1

# ---------- state: seed once, then validate every run ----------
[ -f "$STATE_SRC/reintro-plan.json" ] || die "missing $STATE_SRC/reintro-plan.json"
JUDGE="$STATE_SRC/smoke_judge.py"; [ -f "$JUDGE" ] || die "missing $JUDGE"
plan_ok(){ python3 - "$1" <<'PY' 2>/dev/null
import json,re,sys
p=json.load(open(sys.argv[1]))
ok=lambda t: isinstance(t,str) and re.fullmatch(r'[a-z0-9_]{1,64}',t)
assert isinstance(p,dict) and isinstance(p.get('core'),list) and p['core'] and all(ok(t) for t in p['core'])
assert isinstance(p.get('batches'),list) and p['batches'] and all(isinstance(b,list) and b and all(ok(t) for t in b) for b in p['batches'])
allt=p['core']+[t for b in p['batches'] for t in b]
assert len(allt)==len(set(allt)), 'duplicate tool names'
PY
}
plan_ok "$STATE_SRC/reintro-plan.json" || die "repo copy of reintro-plan.json is invalid"
# one value on one line; "6\n7" must read as corrupt, not as stage 67
read_stage(){ local c; c="$(tr -d ' \t\r' 2>/dev/null < "$STATE/reintro-stage.txt")"
  case "$c" in *"$NL"*) echo multi;; *) printf '%s\n' "$c";; esac; }
NL='
'
is_uint(){ case "$1" in ''|*[!0123456789]*) return 1;; esac; [ "${#1}" -le 6 ]; }
validate_state(){   # called at start and before every cycle: state on a USB drive can vanish or rot
  local s0 seed from f
  for f in reintro-stage.txt reintro-stage.txt.bak reintro-plan.json mcp.json env.sh; do
    if [ -L "$STATE/$f" ]; then log "WARN: $f on the drive is a symlink — removing the link (never writing through it)"; rm -f "$STATE/$f"; fi
  done
  if [ -d "$STATE/reintro-plan.json" ]; then log "WARN: plan path is a directory — moving it aside"; mv "$STATE/reintro-plan.json" "$STATE/reintro-plan.json.bad-$$-$RANDOM"; fi
  if [ ! -f "$STATE/reintro-plan.json" ] || ! plan_ok "$STATE/reintro-plan.json"; then
    [ -f "$STATE/reintro-plan.json" ] && { log "WARN: drive plan corrupt — reseeding from repo"; mv "$STATE/reintro-plan.json" "$STATE/reintro-plan.json.bad-$$-$RANDOM"; }
    cp "$STATE_SRC/reintro-plan.json" "$STATE/" || die "cannot write plan to $STATE"
  fi
  NB=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['batches']))" "$STATE/reintro-plan.json")
  if [ -d "$STATE/reintro-stage.txt" ] || { [ -e "$STATE/reintro-stage.txt" ] && [ ! -r "$STATE/reintro-stage.txt" ]; }; then
    log "WARN: stage path is a directory or unreadable — moving it aside"
    mv "$STATE/reintro-stage.txt" "$STATE/reintro-stage.txt.bad-$$-$RANDOM" 2>/dev/null || rm -rf "$STATE/reintro-stage.txt"
  fi
  s0="$(read_stage)"
  if ! is_uint "$s0"; then
    seed="$(tr -d ' \t\r\n' 2>/dev/null < "$STATE/reintro-stage.txt.bak")"; from=backup
    is_uint "$seed" || { seed="$(tr -d ' \t\r\n' 2>/dev/null < "$STATE_SRC/reintro-stage.txt")"; from=repo; }
    is_uint "$seed" || { seed=0; from=default; }
    if [ -e "$STATE/reintro-stage.txt" ]; then log "WARN: stage file corrupt ('$(printf %.20s "$s0")') — restoring $seed from $from"
    elif [ -n "${VALIDATED:-}" ]; then log "WARN: stage file vanished — restoring $seed from $from"; fi
    rm -f "$STATE/reintro-stage.txt"; echo "$seed" > "$STATE/reintro-stage.txt" || die "cannot write stage to $STATE"
  fi
  s0="$(read_stage)"
  if [ "$s0" != "$((10#$s0))" ]; then s0=$((10#$s0)); rm -f "$STATE/reintro-stage.txt"; echo "$s0" > "$STATE/reintro-stage.txt"; fi
  if [ "$s0" -gt "$NB" ]; then log "WARN: stage $s0 > $NB batches — clamping"; echo "$NB" > "$STATE/reintro-stage.txt"; fi
  VALIDATED=1
}
VALIDATED=""
validate_state
prune_runs(){ local all n d
  all="$(ls -1d "$RUNS"/*/ 2>/dev/null | sort)"; n=$(printf '%s\n' "$all" | grep -c .)
  [ "$KEEP_RUNS" -gt 0 ] && [ "$n" -gt "$KEEP_RUNS" ] || return 0
  printf '%s\n' "$all" | head -n $((n-KEEP_RUNS)) | while IFS= read -r d; do
    [ "${d%/}" = "$OUT" ] || rm -rf "${d%/}"; done
  log "pruned $((n-KEEP_RUNS)) old run folder(s) (KEEP_RUNS=$KEEP_RUNS)"; }
log "AVRONE autopilot  repo=$REPO  drive=$DRIVE  model=$MODEL  stage=$(read_stage)/$NB  pid=$$"

# RAM sanity for the big models (warn only; Apple Silicon unified memory)
case "$MODEL" in gpt-oss:120b*) need_ram=72;; qwen3-coder-next*) need_ram=60;; *70b*|*72b*) need_ram=48;; *) need_ram=0;; esac
ram_gb="${AUTOPILOT_RAM_GB:-}"
if [ -z "$ram_gb" ]; then
  if command -v sysctl >/dev/null && sysctl -n hw.memsize >/dev/null 2>&1; then ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  elif [ -r /proc/meminfo ]; then ram_gb=$(awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo); fi
fi
if [ "$need_ram" -gt 0 ] && is_uint "${ram_gb:-x}" && [ "$ram_gb" -lt "$need_ram" ]; then
  log "WARN: $MODEL wants ~${need_ram}GB RAM but this machine has ${ram_gb}GB — expect swapping or OOM; try MODEL=qwen3-coder:30b"
fi
if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" != arm64 ]; then log "WARN: not Apple Silicon ($(uname -m)) — local models will be very slow"; fi
# keep a Mac awake while we run (caffeinate exits with us), and survive the terminal closing
if command -v caffeinate >/dev/null; then caffeinate -ims -w $$ >/dev/null 2>&1 & fi
trap 'log "SIGHUP (terminal closed) — ignoring, still running; stop with: kill $$"' HUP
BREW_OK=""
need_brew(){ command -v brew >/dev/null || die "Homebrew is required to install $1 — get it at https://brew.sh"; }
if [ "${AUTOPILOT_TIMEOUT_IMPL:-}" = perl ]; then TO=perl_timeout
elif command -v timeout >/dev/null; then TO=timeout
elif command -v gtimeout >/dev/null; then TO=gtimeout
elif command -v brew >/dev/null && brew install coreutils >/dev/null 2>&1 && command -v gtimeout >/dev/null; then TO=gtimeout
else TO=perl_timeout; fi
# child gets its own process group so a timeout also kills its grandchildren (pytest clones etc.)
perl_timeout(){ local s="$1"; shift; perl -e 'my $s=shift; my $p=fork; if(!$p){setpgrp(0,0); exec @ARGV or exit 127} $SIG{ALRM}=sub{kill "-TERM",$p; sleep 2; kill "-KILL",$p; exit 124}; $SIG{TERM}=sub{kill "-TERM",$p; exit 143}; alarm $s; waitpid($p,0); exit($?>>8)' "$s" "$@"; }
: "$BREW_OK"

# ---------- 1. repo hygiene + carried patches ----------
cd "$REPO" || die "cannot cd to $REPO"
git rev-parse --git-dir >/dev/null 2>&1 || die "no git repo at $REPO"
GITDIR="$(cd "$(git rev-parse --git-dir)" && pwd)"
RLOCK="$GITDIR/avrone-autopilot.lock"
if ! mkdir "$RLOCK" 2>/dev/null; then
  rother="$(cat "$RLOCK/pid" 2>/dev/null)"
  if [ -n "$rother" ] && [ "$rother" != "$$" ] && kill -0 "$rother" 2>/dev/null; then
    RLOCK=""; log "another autopilot (pid $rother) is already using this repo (different drive?) — exiting"; exit 2
  fi
  rm -rf "$RLOCK"; mkdir "$RLOCK" 2>/dev/null || { RLOCK=""; log "lost the repo lock race — exiting"; exit 2; }
fi
echo $$ > "$RLOCK/pid"
if [ -f "$GITDIR/index.lock" ]; then
  if pgrep -x git >/dev/null 2>&1; then die "git is running on this machine and $GITDIR/index.lock exists — wait for it, then rerun"; fi
  log "WARN: stale git index.lock (crashed git?) — removing it"; rm -f "$GITDIR/index.lock"
fi
[ -d "$(git rev-parse --git-dir)/rebase-apply" ] && { log "WARN: leftover git am in progress — aborting it"; git am --abort 2>/dev/null; }
# agents/ is AVRONE's runtime per-agent home (agentfs: <workspace>/agents). It is recreated on every
# run, so never delete it — keep it out of git via the clone-local exclude file instead.
EXCL="$(git rev-parse --git-dir)/info/exclude"; mkdir -p "$(dirname "$EXCL")"
grep -qxF '/agents/' "$EXCL" 2>/dev/null || { echo '/agents/' >> "$EXCL"; log "agents/ (runtime agent homes) added to .git/info/exclude"; }
for f in MERGE_ME.md MERGE_ME.md.tmp; do grep -qxF "/$f" "$EXCL" || echo "/$f" >> "$EXCL"; done
if git ls-files --error-unmatch agents >/dev/null 2>&1; then log "agents/ is tracked in git — left untouched"; fi
if ! { grep -q "def apply_tool_focus" avrone/bootstrap.py && grep -q "def coerce_args" avrone/trinity.py; }; then
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no | head -10
    die "uncommitted changes in tracked files — commit or stash them first (nothing was touched)"
  fi
  log "applying carried patches (TOOLS_ONLY, coerce_args)"
  ls "$STATE_SRC"/patches/*.patch >/dev/null 2>&1 || die "patches missing from $STATE_SRC/patches"
  git -c user.name=avrone -c user.email=avrone@local am --3way "$STATE_SRC"/patches/*.patch >> "$OUT/patch.log" 2>&1 \
    || { git am --abort 2>/dev/null; die "patch apply failed (see $OUT/patch.log) — tree restored, resolve manually"; }
  { grep -q "def apply_tool_focus" avrone/bootstrap.py && grep -q "def coerce_args" avrone/trinity.py; } || die "patches applied but symbols still missing"
fi

# ---------- 2. LLM bridge: Ollama with big context ----------
command -v ollama >/dev/null || { need_brew ollama; log "installing ollama"; brew install ollama || die "brew install ollama failed"; }
export OLLAMA_MODELS="$DRIVE/ollama-models" OLLAMA_CONTEXT_LENGTH="$CTX" OLLAMA_KEEP_ALIVE=2h
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"
command -v launchctl >/dev/null && { launchctl setenv OLLAMA_MODELS "$OLLAMA_MODELS" 2>/dev/null; launchctl setenv OLLAMA_CONTEXT_LENGTH "$CTX" 2>/dev/null; }
ollama_up(){ curl -s -m 5 "$OURL/api/version" >/dev/null 2>&1; }
port_open(){ python3 -c "import socket,sys;h,p=sys.argv[1].rsplit(':',1);s=socket.socket();s.settimeout(2);sys.exit(0 if s.connect_ex((h,int(p)))==0 else 1)" "$OHOST" 2>/dev/null; }
OLLAMA_RESTARTS=0
ensure_ollama(){
  ollama_up && return 0
  [ "$1" = "restart" ] && { OLLAMA_RESTARTS=$((OLLAMA_RESTARTS+1)); log "WARN: ollama is down — restarting (#$OLLAMA_RESTARTS)"; }
  [ "$OLLAMA_RESTARTS" -gt 3 ] && die "ollama keeps dying (>3 restarts); see $DRIVE/ollama.log"
  [ -n "$OLLAMA_PID" ] && { kill "$OLLAMA_PID" 2>/dev/null; sleep 1; kill -9 "$OLLAMA_PID" 2>/dev/null; }
  if port_open; then
    die "something on $OHOST accepts connections but does not answer (hung ollama?) — quit it (pkill ollama / quit the Ollama app) and rerun"
  fi
  log "starting ollama serve on $OHOST (ctx=$CTX)"
  nohup ollama serve >> "$DRIVE/ollama.log" 2>&1 &
  OLLAMA_PID=$!
  for _ in $(seq 1 40); do ollama_up && return 0; kill -0 "$OLLAMA_PID" 2>/dev/null || break; sleep 1; done
  die "ollama did not start; see $DRIVE/ollama.log"
}
ensure_ollama start
ensure_model(){
  ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qxF "$MODEL" && return 0
  local need_gb free_gb ok attempt
  need_gb="${MIN_FREE_GB:-80}"
  free_gb=$(df -Pk "$DRIVE" | awk 'NR==2{print int($4/1048576)}')
  [ "${free_gb:-0}" -ge "$need_gb" ] || die "not enough space on $DRIVE for $MODEL: ${free_gb}GB free, need ${need_gb}GB (set MIN_FREE_GB to override)"
  log "pulling $MODEL (large; ${free_gb}GB free)"
  ok=""; for attempt in 1 2 3; do bg_wait ollama pull "$MODEL" && { ok=1; break; }; log "WARN: pull attempt $attempt failed"; sleep $((attempt*5)); done
  [ -n "$ok" ] || die "could not pull $MODEL after 3 attempts"
}
ensure_model
warm="$($TO "${WARM_TIMEOUT:-900}" ollama run "$MODEL" 'reply with the single word ready' </dev/null 2>/dev/null | tr -d '\n' | head -c 60)"
[ -n "$warm" ] || die "$MODEL produced no output on warm-up within ${WARM_TIMEOUT:-900}s; check $DRIVE/ollama.log (out of memory?)"
log "warming $MODEL: $warm"

# ---------- 3. install + baseline gate ----------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null && { PY="$c"; break; }
done
if [ -z "$PY" ]; then need_brew python@3.12; log "installing python@3.12 (AVRONE needs >= 3.11)"; brew install python@3.12 || die "python install failed"; PY="$(brew --prefix)/bin/python3.12"; fi
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then log "rebuilding old/broken .venv"; rm -rf .venv; fi
[ -d .venv ] || "$PY" -m venv .venv || { rm -rf .venv; die "venv creation failed for $PY"; }
[ -f .venv/bin/activate ] || { rm -rf .venv; die "broken .venv (no activate) — rerun"; }
# shellcheck disable=SC1091
source .venv/bin/activate
if [ "${AUTOPILOT_NO_INSTALL:-0}" != "1" ]; then bg_wait pip install -q -e ".[dev]" > "$OUT/pip.log" 2>&1 || die "pip install failed (see $OUT/pip.log)"; fi
if [ "${SKIP_GATE:-0}" != "1" ]; then
  GATE_CMD="${GATE_CMD:-python -m pytest -q -p no:cacheprovider -m 'not integration'}"
  log "baseline gate: $GATE_CMD"
  if ! bg_wait bash -c "$GATE_CMD" > "$OUT/baseline.txt" 2>&1; then
    log "baseline RED — fix main before evolving:"; tail -20 "$OUT/baseline.txt"; exit 1
  fi
  log "baseline: $(tail -1 "$OUT/baseline.txt")"
fi

# ---------- 4. coherent env for every bridge ----------
q(){ printf '%q' "$1"; }   # paths may contain $, quotes, backticks, spaces: escape for safe sourcing
cat > "$STATE/env.sh" <<EOF
export AVRONE_PROVIDER=ollama AVRONE_LLM_BASE_URL=$(q "$OURL/v1")
export AVRONE_LLM_MODEL=$(q "$MODEL") AVRONE_LEAD_MODEL=$(q "$MODEL") AVRONE_LLM_FALLBACKS=""
export AVRONE_LLM_TIMEOUT=900 AVRONE_LLM_RETRIES=2
export AVRONE_RUNNER=local AVRONE_SELF_CHANGES=allow AVRONE_SELF_EVOLVE=allow
export AVRONE_WORKSPACE=$(q "$REPO") AVRONE_REPO_DIR=$(q "$REPO") AVRONE_SKILLS_DIR=$(q "$REPO/skills") AVRONE_PLUGINS_DIR=$(q "$REPO/plugins")
export AVRONE_MCP_CONFIG=$(q "$STATE/mcp.json")
export AVRONE_RECON_ALLOWLIST=127.0.0.1,localhost
export AVRONE_BRIDGE_MAX_POSTS=1000 AVRONE_MAX_PARALLEL=2
export NO_PROXY=127.0.0.1,localhost
export AVRONE_EVOLVE_TEST_CMD="python -m pytest -q -x -p no:cacheprovider -m 'not integration' tests"
EOF
python3 -c "import json,sys;json.load(open(sys.argv[1]))" "$STATE/mcp.json" 2>/dev/null || echo '{"servers":{}}' > "$STATE/mcp.json"
# shellcheck disable=SC1091
source "$STATE/env.sh"
log "bridges: provider=ollama ($MODEL @ $OHOST)  learning-bridge=bridge.py  fleet=local  codex=on  recon=localhost-only"
AVRONE_DATA_DIR="$DATA/doctor" python -m avrone.cli doctor > "$OUT/doctor.txt" 2>&1 || log "doctor reported warnings (see doctor.txt)"
log "doctor: $(tail -1 "$OUT/doctor.txt")"

# ---------- helpers ----------
tools_for(){ python3 - "$STATE/reintro-plan.json" "$1" <<'PY'
import json,sys;p=json.load(open(sys.argv[1]));s=max(0,min(int(sys.argv[2]),len(p['batches'])));B=p['batches']
cur=p['core']+[t for b in B[:s] for t in b]; nxt=B[s] if s<len(B) else []
print(','.join(cur)+' '+(','.join(nxt) or '-'))
PY
}

reintro_step(){   # Track A: smoke-test the next batch; advance when coherent
  local S CUR NEXT P
  S="$(read_stage)"
  read -r CUR NEXT < <(tools_for "$S")
  [ "$NEXT" = "-" ] && { log "reintro complete: $NB/$NB, all $(echo "$CUR" | tr , '\n' | wc -l | tr -d ' ') tools active"; return 0; }
  log "reintro stage $S/$NB — smoke-testing: $NEXT"
  P="Tool check. Reply ONLY with one JSON object {\"tool\":NAME,\"args\":{...}} per turn. Call each of these tools exactly once with small, safe, valid arguments (read each tool's args doc; use path '.', text 'hello', host 127.0.0.1 where needed): $NEXT. Guardrails: port_scan only 127.0.0.1 with authorized=true and <=5 ports; shell/priv_shell/priv_admin only read-only commands (id, uname -a, df -h); fs_delete only a file you created; docker_run/env_install only if available (an 'unavailable' error counts as exercised); bridge_post then bridge_read then bridge_reply on the same post; spawn one tiny sub-agent with a one-line task. Finish with one line per tool: NAME OK or FAIL."
  AVRONE_TOOLS_ONLY="$CUR,$NEXT" AVRONE_MAX_STEPS=$(( $(echo "$NEXT" | tr , '\n' | wc -l) * 2 + 6 )) \
  AVRONE_DATA_DIR="$DATA/smoke" bg_wait $TO "${SMOKE_TIMEOUT:-1800}" python -m avrone.cli chat "$P" > "$OUT/smoke-$S.txt" 2> "$OUT/smoke-$S.err"
  python3 "$JUDGE" "$OUT/smoke-$S.err" "$NEXT" "$STATE/reintro-stage.txt" "$S" | tee -a "$OUT/autopilot.log"
}

COMMON='You are one lane of the AVRONE self-improvement squad. Reply ONLY with one JSON object {"tool":NAME,"args":{...}} per turn. Start with evolve_list; if it already shows two ACCEPTED proposals for your lane goal, finish. Read only real paths you have seen in a listing (list_files/read_file; if paths are prefixed shared/ in listings, evolve_propose paths are repo-relative WITHOUT shared/). Rules: create NEW files only, small and genuinely useful, stdlib or installed deps, offline tests only (patch network). Never write repo files with write_file/edit_file — put them only in evolve_propose {"goal":..., "edits":[{"path":..., "content":FULL FILE}]}. The harness runs the full non-integration suite in an isolated clone and keeps it only if ALL tests pass. If REJECTED, read the test tail, fix YOUR file, resubmit. You may run python/shell to test locally first, and post findings to the learning bridge (bridge_post) so other lanes see them. Finish after two ACCEPTED, then call evolve_list and report each id + status. Your lane: '

lane(){   # Track B
  case " $LANES " in *" $1 "*) ;; *) return 0;; esac
  local d="$DATA/lane-$1" CUR rc
  check_drive
  mkdir -p "$d"
  read -r CUR _ < <(tools_for "$(read_stage)")
  log "lane $1 start ($(echo "$CUR" | tr , '\n' | wc -l | tr -d ' ') tools)"
  AVRONE_TOOLS_ONLY="$CUR" AVRONE_MAX_STEPS=96 AVRONE_DATA_DIR="$d" \
    $TO "$LANE_TIMEOUT" python -m avrone.cli chat "$COMMON$2" --json "$OUT/$1.json" > "$OUT/$1.txt" 2> "$OUT/$1.err"
  rc=$?
  log "lane $1 rc=$rc$([ "$rc" = 124 ] && echo ' (timed out)') calls=$(grep -c 'tool.call' "$OUT/$1.err") proposals=$(grep -c 'evolve_propose' "$OUT/$1.err") accepted=$(grep -ci 'ACCEPTED' "$OUT/$1.err")"
}

run_pair(){   # wait only on our lane PIDs: a bare `wait` would also block on the backgrounded ollama serve
  local a b
  lane "$1" "$2" & a=$!
  lane "$3" "$4" & b=$!
  LANE_PIDS="$a $b"
  wait_pid "$a"; wait_pid "$b"
  LANE_PIDS=""
}

squad(){
  run_pair deepcov "DEEP COVERAGE: avrone/trinity.py, avrone/evolve.py, avrone/gateway.py uncovered error/edge branches. New files like tests/test_trinity_deep_r6.py. Land TWO accepted." \
           newmods "COVERAGE for newest modules: avrone/governor.py, greatscale.py, checkpoint.py, memstore.py, agentfs.py, bridge.py. New files tests/test_<module>_r6.py. Land TWO accepted."
  ensure_ollama restart
  run_pair tools "NEW TOOLS: read plugins/calculator.py for the register(registry, settings) shape; add plugins/hash_tool.py (hash_text {text, algo}) and plugins/agent_memory.py, each with its own focused test file. Land TWO accepted." \
           robust "ROBUSTNESS: avrone/recon.py (default-deny allowlist), osint.py, fleet.py, jobqueue.py edge cases, offline. Land TWO accepted."
}

report(){
  local tmp="$REPO/MERGE_ME.md.tmp" d
  { echo "# AVRONE — accepted proposals awaiting review"; echo
    echo "Generated $(date). Review with: AVRONE_DATA_DIR=<lane data dir> python -m avrone.cli evolve diff <id>. Do NOT merge without Crow's go-ahead."; echo
    echo "Reintro stage: $(read_stage)/$NB"; echo; echo '```'
    for d in "$DATA"/lane-*; do [ -d "$d" ] || continue; echo "## $d"; AVRONE_DATA_DIR="$d" python -m avrone.cli evolve 2>/dev/null; done
    echo '```'; } > "$tmp" && mv "$tmp" "$REPO/MERGE_ME.md"
  cp "$REPO/MERGE_ME.md" "$OUT/MERGE_ME.md"
  log "summary written: MERGE_ME.md (and $OUT)"
}

# ---------- 5. the loop ----------
n=0
while :; do
  n=$((n+1)); CYCLE_N=$n; log "=== cycle $n ==="
  free_now=$(df -Pk "$DRIVE" 2>/dev/null | awk 'NR==2{print int($4/1048576)}')
  [ "${free_now:-0}" -ge "${MIN_RUN_FREE_GB:-5}" ] || die "drive $DRIVE is nearly full (${free_now:-?}GB free, need ${MIN_RUN_FREE_GB:-5}GB) — free space and rerun; state is saved"
  prune_runs
  check_drive
  validate_state
  ensure_ollama restart
  ensure_model
  PHASE=smoke; reintro_step
  check_drive
  check_drive
  PHASE=lanes; squad
  check_drive
  PHASE="report"; report; PHASE="sleep"
  [ "$CYCLES" != "0" ] && [ "$n" -ge "$CYCLES" ] && break
  sleep "${CYCLE_SLEEP:-30}" & SLEEP_PID=$!; wait "$SLEEP_PID"; SLEEP_PID=""   # backgrounded so TERM/INT act immediately
done
log "done. results in $OUT"
exit 0
