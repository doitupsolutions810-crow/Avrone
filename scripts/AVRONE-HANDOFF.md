# AVRONE — Operator Handoff

You are taking over an autonomous, self-evolving AI system called **AVRONE**. You may have
zero prior knowledge of this project. This document plus `mac-avrone-autopilot.sh` is
everything you need to find the system, configure it and its bridges, install the right
model, and continue the work **exactly where it was left off**. Read this top to bottom
before doing anything.

---

## 1. What AVRONE is

AVRONE is a Python (FastAPI) multi-agent platform that can **edit its own source code**
safely. An agent proposes code changes; a sandboxed harness (`avrone/evolve.py`) applies
them in a throwaway git clone, runs the full test suite, and **keeps the change only if
every test passes** — otherwise it hands back the failing output to try again. Nothing ever
touches `main` or the live tree automatically; accepted work lands on `evolve/<id>`
branches for a human to review and merge.

Architecture (the three layers from the original design):

- **Eternal CodeX** — `avrone/codex.py`: an event bus + append-only JSONL run logs. Every
  tool call, agent step and decision is recorded here. This is AVRONE's memory of what it did.
- **Trinity Core** — `avrone/trinity.py`: the kernel. Planner → parallel **squad** of worker
  agents → a **Lead** that cross-checks and merges. Runs the JSON tool loop and spawns
  sub-agents.
- **Ghost Shell** — `avrone/server.py` + `static/index.html`: REST API, a WebSocket HUD, and
  an admin terminal.

Supporting modules you will touch: `sovereign.py` (Docker/local runner), `gateway.py`
(multi-provider LLM failover + circuit breaker), `fleet.py` / `presence.py` (multi-node
mesh), and the **bridges** (see §3).

The full, authoritative module-by-module history is in `README.md` and in the test suite
under `tests/` (~2,300+ tests). When in doubt, read the code and the tests — never guess a
path or an API.

## 2. The mission (what "continue the work" means)

Two tracks are in flight. Your job is to keep both moving and leave reviewable results.

**Track A — tool reintroduction.** AVRONE has **75 tools**. On weak/local models the full
registry overwhelms a small context, so we trim it with `AVRONE_TOOLS_ONLY` and re-add tools
in **9 staged batches**, smoke-testing each batch before admitting it. The plan is
`scripts/avrone-state/reintro-plan.json`; the current stage is in
`scripts/avrone-state/reintro-stage.txt`.

> **Left off at: stage 7 of 9 — 58 of 75 tools active.** Stages 0–6 passed. Stage 7 (the
> `spawn / fleet_status / bridge_post / bridge_read / bridge_reply / codex_search /
> codex_stats` batch) kept **holding** on the small 7B model because it emitted malformed or
> empty calls. A capable model (see §4) should clear stages 7, 8 and 9 and reach the full
> 75-tool registry.

**Track B — self-evolution squad.** Run parallel evolve lanes. Each lane reads a real
module, finds a genuinely uncovered branch or a small useful new tool, and submits
`evolve_propose {goal, edits:[{path, content}]}`. The harness gate-tests it in isolation and
accepts only all-green changes onto an `evolve/` branch.

> **Left off at: 0 accepted proposals.** The 7B model made many calls but landed **nothing**
> — it is too weak. This is the single biggest reason to move to a bigger model. The gate
> itself is healthy: it has correctly accepted good changes and rejected broken ones in past
> rounds on stronger models.

**Definition of done for a cycle:** reintro stage reaches 9/9 (75 tools), and the evolve
lanes land at least a few **accepted** proposals on `evolve/` branches, summarized in
`MERGE_ME.md` for the human to review. **Never merge to `main` yourself** and **never push
without the human's go-ahead.**

## 3. The bridges — find and configure them coherently

"Bridges" in AVRONE means the connectors that let parts of the system talk. Configure all of
them before running; the autopilot script does this and self-checks it:

1. **LLM/provider bridge** (`avrone/gateway.py` + `avrone/llm.py`): AVRONE → the model. Here
   it points at **local Ollama** (`AVRONE_PROVIDER=ollama`,
   `AVRONE_LLM_BASE_URL=http://127.0.0.1:11434/v1`). No cloud, no API key, no cost. Verify
   Ollama answers `/api/version` and that the model is pulled.
2. **Learning bridge** (`avrone/bridge.py`): a run-scoped blackboard where every agent in a
   run (squad workers, their sub-agents, deep reviewers) posts findings/questions/answers/
   conclusions and reads each other's. Persisted as JSONL under `<data_dir>/bridge/<run_id>`.
   Tools: `bridge_post`, `bridge_read`, `bridge_reply`. These are part of stage-7 reintro.
3. **Fleet bridge** (`avrone/fleet.py` + `presence.py`): HMAC-signed node-to-node mesh for
   spawning/relaying across machines. Single-machine autopilot keeps this local
   (`AVRONE_RUNNER=local`); no remote nodes are required.
4. **CodeX bridge** (`avrone/codex.py`): the event/log bus every run writes to; `codex_search`
   / `codex_stats` read it (also stage-7 reintro).

Coherent config = one env block (the autopilot writes it to `avrone-state/env.sh`) with the
provider pointed at local Ollama, data dir on persistent storage, repo/workspace/skills/
plugins dirs all pointing at this checkout, self-change and self-evolve **allowed**, and
recon kept **default-deny** except `127.0.0.1` (guardrail: `port_scan` on localhost only).
Run `python -m avrone.cli doctor` to self-check the wiring before the loop starts.

## 4. The model — install the bigger one

The small `qwen2.5-coder:7b` is the proven bottleneck. On this Mac (Apple Silicon, ~128 GB)
install a frontier-class **free, local** model via Ollama. Default and recommendation:

- **`gpt-oss:120b`** — OpenAI's open-weight 120B (MXFP4, ~65 GB), strong tool-calling and
  reasoning, fits comfortably in 128 GB. This is the default the autopilot pulls.
- Alternative if you prefer a coder-specialist: `qwen3-coder-next:q4_K_M` (~52 GB).

Always run Ollama with a large context so the 75-tool prompt is never truncated
(`OLLAMA_CONTEXT_LENGTH=32768`). Everything stays on-device; there is no paid cloud.

## 5. Guardrails (do not cross)

- Self-change/self-evolve are allowed **because the owner (Crow) approved agents changing
  their own sandbox**, but accepted code lands only on `evolve/` branches, never `main`.
- Recon/OSINT is **default-deny**; the only allowlisted target is `127.0.0.1`, and
  `port_scan` requires `authorized=true` with ≤5 ports on localhost. Do not scan anything
  else.
- Do not delete committed source. `agents/` in the repo is AVRONE's runtime per-agent home
  (recreated every run). The autopilot keeps it and hides it from git via `.git/info/exclude`;
  never delete it.
- Everything is logged (CodeX + resources ledger). Keep it that way.

## 6. How to run

```bash
# one command, from the repo root, with your external drive mounted:
DRIVE=/Volumes/AI ./scripts/mac-avrone-autopilot.sh
```

It will: locate/validate the checkout → apply the two carried patches if the code lacks them
(see §7) → install/start Ollama with big context → pull the model → install AVRONE and run
the **baseline gate** (must be green before anything evolves) → write and self-check the
coherent env + bridges → then **loop**: advance the reintro stage and run the evolve squad
lanes, writing results to `$DRIVE/avrone-runs/` and accepted proposals to `MERGE_ME.md`.

Exit codes: 0 done, 1 config/precondition error (message says what), 2 another autopilot already
running on that drive, 130/143 interrupted (lock released, child processes stopped). It is safe to
rerun at any time: state resumes from the drive, corrupt state files are reseeded, a stale lock is
recovered, and Ollama is restarted if it dies (gives up after 3 restarts).

More resilience: state is re-validated before **every** cycle (a stage file deleted or corrupted
mid-run is restored from `reintro-stage.txt.bak`, written on each advance). A hanging lane is killed
at `LANE_TIMEOUT` and a crashing lane is logged; the loop continues either way. It refuses to apply
patches over uncommitted edits to tracked files (your work is never touched). The Mac is kept awake
with `caffeinate`, closing the terminal (SIGHUP) does not stop it, and it warns when RAM is too small
for the chosen model. The stage-advance rule lives in `scripts/avrone-state/smoke_judge.py`
(>=60% of the batch called, <=3 malformed/unknown calls).

Hostile-environment guards: it stops cleanly (exit 1, state intact) if the drive is unplugged or
swapped mid-run and never recreates the mount point; refuses `DRIVE=/`, your home folder, or a path
inside the repo; refuses to run as root (`ALLOW_ROOT=1` to force); rejects shell metacharacters in
`MODEL`, `LANES`, `OLLAMA_HOST` (they are written into env.sh); restarts its own hung Ollama but
refuses to fight a foreign hung listener on the port; times out a frozen warm-up (`WARM_TIMEOUT`,
default 900s); and re-pulls the model if it is deleted mid-run.

Useful switches: `MODEL=gpt-oss:120b` (default), `ONCE=1` (one cycle then stop),
`CYCLES=N` (stop after N cycles; default: loop forever), `SKIP_GATE=1` (not recommended),
`LANES="deepcov newmods tools robust"`.

## 7. Carried patches (why they're here)

Two improvements were made off-cloud and are **not yet on GitHub**:

- `0001 — AVRONE_TOOLS_ONLY`: the registry-trimming used by reintro.
- `0002 — trinity.coerce_args`: repairs flattened/aliased tool args from small models
  (`command→cmd`, `file_path→path`, `text→content`, …).

They live in `scripts/avrone-state/patches/`. The autopilot applies them only if the running
code doesn't already contain them (idempotent; it checks for the symbols first). If you
reconnect GitHub and push `main`, a fresh clone will already have them and the patch step
becomes a no-op.

## 8. When you're done

Leave the system with: the highest reintro stage you reached, the baseline + lane logs under
`$DRIVE/avrone-runs/<timestamp>/`, and `MERGE_ME.md` listing each accepted proposal id with
its goal and branch. Report that summary back to Crow for review and merge. Do not push or
merge without his word.
