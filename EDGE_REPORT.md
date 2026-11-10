# AVRONE edge-fix report

Branch: `fix/macos-and-edge` (local only — not pushed, not merged to main).
Working tree changes are uncommitted. Date: 2026-11-10, America/Chicago.

Ran on this Linux box from `/workspace/avrone-run` (clone of
https://github.com/doitupsolutions810-crow/Avrone), with `AVRONE_LLM_MODE=mock`.
Ollama/qwen3 and Docker were not required; no tests had to be skipped for
missing external services.

## Failures found and fixes

### 1. `tests/test_governor.py::test_proc_meminfo_reader`
**Cause.** `avrone/governor.py` `read_mem_pct()` opened `/proc/meminfo` with no
guard (after a failed/absent psutil path), so platforms without that file raised
`FileNotFoundError`. The test already accepts `None`.

**Fix (cross-platform, Linux behaviour kept).**
- Wrap the `/proc/meminfo` read in `_read_mem_pct_proc()` and return `None` on
  `OSError`.
- Skip unparsable lines (`ValueError`/`IndexError`) so a malformed file cannot
  crash the governor; empty or zero-total files still return `None`.
- Fall back to `_read_mem_pct_macos()` (`sysctl hw.memsize` + `vm_stat`) when
  `/proc` is absent; that helper returns `None` on non-Darwin.

### 2. `tests/test_mcp_plugins.py::test_avrone_mcp_server_over_stdio` (macOS hang)
**Cause.** `LocalRunner._exec` spawned `bash -lc "echo ready"` (and any other
command) without `stdin=DEVNULL`, so the health-check child inherited the MCP
server's JSON-RPC stdin and did not finish until the 60s timeout.

**Fix.** In `avrone/sovereign.py` `_exec`, pass
`stdin=asyncio.subprocess.DEVNULL` to `create_subprocess_exec`.

### 3. MCP stdio server hung after valid replies (exposed by extra edge tests)
**Cause.** `serve()` calls `warm()` (boots the core in the background). Short
clients (ping, parse error, empty stdin) close stdin before `build_core`
finishes. `shutdown()` previously only stopped extras when `_core_task` was
already done, so a still-booting core was left running. `asyncio.run` then
cancelled that task, raced the health-check subprocess transport, and
`_reap`/`proc.wait()` never woke — hang inside `_cancel_all_tasks`.

A second, environment-specific issue: `connect_read_pipe(sys.stdin)` on a char
device like `/dev/null` failed to register with epoll
(`PermissionError: Operation not permitted`), so EOF was never delivered.

**Fix.**
- `shutdown()` cancels and awaits an in-flight `_core_task`.
- `build_core()` stops a partially started runner/MCP manager if boot is
  cancelled.
- `_reap()` (and the `_exec` `CancelledError` path) bound `proc.wait()` with
  `asyncio.wait_for(..., timeout=2)` so a dead transport cannot pin the loop.
- `amain()` closes the stdin read-pipe transport after `serve()` returns.
- `_stdin_already_eof()` treats a non-tty char-device stdin (`/dev/null`) as
  immediate EOF and skips `connect_read_pipe`.

### 4. `tests/test_recon_extra.py::test_port_cap_authorized_gate`
**Cause.** This box's DNS maps `example.com` to `198.18.0.1` (RFC 2544
benchmark range). The production gate correctly refuses non-public resolved
addresses. The test used live DNS; sibling modules already stub `_resolve_ips`.

**Fix (test only — security check left intact).** Stub
`recon._resolve_ips` to return `93.184.216.34`, matching
`tests/test_recon_offline.py` / `tests/test_recon_hardening.py`. The authorized
flag, 32-port cap, and mocked "open: none" assertions are unchanged.

## Extra edge tests (new files only)

Added under `tests/edge_extra/` (20 tests):

- Governor: `/proc/meminfo` missing, unreadable, empty, malformed, mixed
  bad lines with a valid fallback, zero total; macOS fallback hook; Linux
  happy path still works; `make_disk_reader` on missing relative, missing
  absolute, and empty paths.
- MCP stdio (each run wrapped in a subprocess timeout): immediate EOF,
  malformed JSON (`-32700`), batch array, empty batch (`-32600`), unknown
  method (`-32601`), `tools/call` with bad arguments (non-object args,
  non-string task, unknown tool, missing task, non-object params), ~1 MiB
  valid request, and a line over the 16 MiB StreamReader limit (server
  reports `-32600` and still answers a later ping). Every stdout line must
  be valid JSON-RPC.

## Pytest summaries

Full non-integration gate (project command, including edge tests):

```
python -m pytest -q -p no:cacheprovider -p no:randomly -m 'not integration' --timeout=120
3353 passed, 362 deselected, 1 warning in 56.07s
```

Same gate with pytest-randomly shuffled order (`-p no:cacheprovider`, plugin
auto-loaded):

```
3353 passed, 362 deselected, 1 warning in 54.06s
```

Edge tests only:

```
python -m pytest -q -p no:cacheprovider -p no:randomly tests/edge_extra --timeout=60
20 passed in 1.10s
```

Earlier gate, before adding `tests/edge_extra/`: `3333 passed, 362 deselected`
(after the four fixes above). All 0 failed, 0 errored, 0 skipped.

## Skips / deselected

- **0 skipped** in the final runs.
- `tests/test_agentkit.py::test_qr_roundtrip` initially skipped
  (`zxingcpp` missing). Installed optional extra `zxing-cpp` (listed under
  `[project.optional-dependencies] vision`); the test then passed. Not an
  external service.
- **362 deselected** with `-m 'not integration'` — the project's own gate
  marker for real-network/uvicorn tests. Not skipped for missing Ollama or
  Docker. `AVRONE_LLM_MODE=mock` was set so unit tests that talk to the LLM
  layer use the mock.

## git diff --stat (uncommitted, branch `fix/macos-and-edge`)

```
 avrone/governor.py        | 68 +++++++++++++++++++++++++++++++++++-----
 avrone/mcp_server.py      | 79 +++++++++++++++++++++++++++++++++++++----------
 avrone/sovereign.py       | 17 +++++++---
 tests/test_recon_extra.py |  3 ++
 4 files changed, 137 insertions(+), 30 deletions(-)
```

Untracked (new tests only):

```
tests/edge_extra/__init__.py
tests/edge_extra/test_governor_edges.py
tests/edge_extra/test_mcp_stdio_edges.py
```

Nothing was pushed. Nothing was merged to `main`.

---

# Round 2 — 2026-11-10, America/Detroit

Still on local branch `fix/macos-and-edge`. Nothing pushed. Nothing merged to `main`.
Working tree remains uncommitted. Surveyed GitHub read-only; no clone/merge of other
repos into this tree.

## Repo inventory (doitupsolutions810-crow)

GitHub search `user:doitupsolutions810-crow` returned **38 repositories** (default
branch `main` on all listed). Local `/workspace/avrone-run` is still at Avrone
`5cceb52` (`Add Avrone GET/POST /api/blackboard for the fusion relay (#20)`,
committed 9:22 AM EST). Avrone `main` has **not** moved past that commit; later
`pushed_at` stamps are from other branches/PRs.

### AVRONE-related (deeper look)

| Repo | Head / recent | Open PRs | Integrate into Avrone? |
|---|---|---|---|
| **Avrone** | `5cceb52` v0.34.0, same as this checkout | 18 open | See ranked candidates below. `#24`, `#25`, `#26` are the ones not yet in this tree. |
| **avrone-daemon** | Very active today; tip `4b59cff` at 6:33 PM EST. Recent tags **v0.137.0–v0.142.0** (hostile-name escaping, fleet.json/task_board no-overwrite, CLI escaping, HUD null-prototype). Plus checklist commits about pip/pytest/doctor/reintro. | At least 20 open (page cap), including `#21–#24` (UTF-8 fusion, claim-file nofollow, loopback breaker, platform runtime bridge) and a stack of trial-receiver PRs | **Owner review, not a merge.** This is a diverged line (0.142 vs Avrone 0.34). Cherry-pick individual hardenings after review. |
| **GreatScale-Avrone-Grok-Fusion-Bridge** | Tip `3f8cc12` (Nov 3, 12:37 PM EST): gap-3 evidence posts, feedback-loop filter, 50-cycle soak. Earlier: docker-compose stack, endpoint resolver, working relay. | 5 open (`#1–#5`): real contracts, verified relay repair, auth relay, evidence adapter, hardened relay | Keep as a **sibling** service. Avrone `#20` (already on main) is the in-tree blackboard hook. Remaining relay PRs need owner review, not a mechanical copy into Avrone. |
| **CycleKernal** | Tip `642bd64` (Nov 9, 11:23 PM EST), merged `#14`: real relay mood, POST `/v1/events`, UTF-8 reject, boot id | 6 open (admin auth, early 413, idempotent chat-save, GS descent, etc.) | Peer of the fusion bridge, not source to merge into Avrone. Contract already matches the bridge work. |
| **living-intermediate-control-plane** | Tip `5db14c9` (early Nov 10): merged `#39` technician auth / X/Y/Z role-gated proxy / dataset override. Also 20 open PRs, including `#42` and `#41` syntax/repair (updated ~6:00 PM EST) | 20 open | **Do not** pull the proxy/override work into Avrone (same declined Control704 scope). Syntax-fix PRs belong in that repo. |
| **great-scale** | `main` tip is Nov 1 (`74cad04` revert atlas to feature branch). `pushed_at` today is likely a branch. 20 open PRs including `#134` unterminated-string syntax fix (5:57 PM EST) and `#133` QGT import syntax | 20 open (page cap) | Research/atlas sibling. Avrone `#15` (Great Scale live research bridge) already open against Avrone main — owner review. Syntax fixes stay in great-scale. |
| **grok-bot-** | Tip `c4d5e95` (Oct 29): Docker socket-proxy allow-list so `docker.info()` boot works; host-gateway routing for the AVRONE bridge | 4 open (`#4` signed Swarm execution, `#3` sync AVRONE hardening, `#2` harden bridge, `#1` GHA live tasks) | Pair with Avrone `#17/#19`. Owner review; do not merge blindly. |
| **swarm-vm-runner** | Tip `9bd1cfb` (Oct 30): isolated runner trust boundary; host Docker socket removed from ephemeral runner | 4 open (`#5` fail-closed success proof, updated today; auth/evidence; token lifecycle) | Infrastructure sibling. Avrone `#26` talks to Swarm boundaries — review together, don't vendor the runner into Avrone. |
| **control12-workspace** | `main` last commit Sep 17 (CI/probe). PR `#8` updated 6:32 PM EST: signed mission gateway | 3 open | Separate workspace product. Not a mechanical Avrone upgrade. |
| **control704-proxy-system** | Present; description mentions “Proxy X, Y, Z” / dataset override | (not used as an integration source) | **Do not integrate** authentication-bypass, override, or “proxy of any type” into Avrone. |

Other owner repos (inventory only, not Avrone upgrades): Aiminer, CycleKernal-adjacent forks, trinity-core, capability-authority, capability-plane, TrainLLM, control12-lattice-ops, attested-history, github-api-automation, control704-grokbot-knowledge, ghostshell / ghostshell-docs, cleanroom-agent-system, and several private or stale named repos (`pilot-cloud-finch-rose`, `onyx-pine-mint-pearl`, `x-near-me`, `local-creator-radar`, `cyclekernel-showcase`, `wallaby-tutorial`, etc.).

### Ranked integration candidates (into Avrone / this tree)

1. **Avrone #24** — *Keep nested blackboard values and non-finite timestamps encodable.* Tight `_json_safe` / `public_post` hardening plus tests. **Mostly mechanical**, still wants a quick owner look at `_remote_ts` behavior. Base: `main`.
2. **Avrone #25** — *Keep poisoned run logs from crashing the JSON response.* Codex `_json_safe`, refuse emit outside the log dir / via symlink, survive NaN and lone surrogates. **Mechanical robustness**, but the PR is stacked on `cursor/avrone-fusion-blackboard-59b2`, not `main` — rebase first. Owner review.
3. **Avrone #26** — *Harden Swarm boundaries and require complete historical CI tests* (updated 6:01 PM EST). Large: `bridge.py`, `codex.py`, `fleet.py`, `plugins/platform_bridge.py`, new verify script and ~1k-line registry, CI, many tests (including a touch of `tests/test_recon_extra.py`). **Needs owner review**, not a mechanical merge. Conflicts likely with this branch's `test_recon_extra.py` stub.
4. **avrone-daemon v0.137–v0.142 hardenings** (escape hostile names in list/search/read/proc_status, memory keys, CLI args, fleet.json/task_board no-overwrite). **Cherry-pick after review**; lines have diverged too far for a merge.
5. **avrone-daemon PRs #21–#24** (fusion 429/UTF-8, claim-file nofollow, loopback breaker, platform runtime). Related to Avrone `#26`. **Owner review.**
6. **Fusion Bridge open PRs #1–#5** + CycleKernal `/v1/events` (already on CycleKernal main). Wire as running services, don't copy into Avrone. **Owner review.**
7. **Avrone #17 / #19** (Grok Bot unified launch, Trinity signed Swarm dispatch) and **grok-bot- #2/#4**. Large integration, stacked branches, **owner review**.
8. **Avrone #15** (Great Scale live research council). Feature branch; **owner review**.

Not recommended: anything from `control704-proxy-system` or the merged living-intermediate `#39` proxy/override path.

## New bugs found and fixed this round

### 5. Linux `/proc/meminfo` used-percent could be outside 0..100
**Cause.** `_read_mem_pct_proc()` (`avrone/governor.py` around the return at line 85) computed `100 * (1 - avail/total)` with no range check. An inverted or negative `MemAvailable` would return e.g. -100 or 150 and quietly skew the governor. The macOS helper already refused out-of-range values (line 123).

**Fix.** Return `None` unless `0.0 <= used <= 100.0`, matching macOS. Locale comma thousands/decimals still fail `float()` and are skipped (existing skip path).

### 6. MCP stdio: deep JSON or non-object requests could hang the protocol
**Cause.** `serve()` only caught `json.JSONDecodeError`. A deeply nested payload can raise `RecursionError` and kill the server. A top-level scalar (`42`) or a non-object batch item was silently ignored (JSON-RPC 2.0 requires Invalid Request).

**Fix (`avrone/mcp_server.py`).**
- Catch `RecursionError` at line 174 and return `-32700`.
- Non-object top-level messages (line 183) and non-object batch items (`_handle_safe`, line 215) return `-32600`.
- Stray JSON-RPC *responses* (objects without `method`) are still ignored.
- Updated `tests/test_mcp_server.py::test_serve_roundtrip_batch_parse_error_and_notifications` so the documented `"42"` case now expects `-32600` (6 messages instead of 5).

### 7. Evolve default gate called bare `python`, which is often not on PATH
**Cause.** `_propose_once` defaulted to `python -m pytest -q -m "not integration"`. On this box (and many macOS/Linux nodes) only `python3` exists unless a venv is activated, so good proposals were `REJECTED` with `python: command not found`. Exposed while running the full gate via `.venv/bin/python` without activating the venv.

**Fix (`avrone/evolve.py`).** New `_default_test_cmd()` at line 167 uses `shlex.quote(sys.executable)` so the gate runs with the same interpreter as AVRONE. `AVRONE_EVOLVE_TEST_CMD` still wins when set. Cross-platform (Linux + macOS).

No recon production assertions were weakened. New recon tests only use mocked DNS/`open_connection`, refuse `127.0.0.1`/`localhost` even with `authorized=true`, and use ≤5 ports on the mocked happy path.

## New edge tests this round (41, all under `tests/edge_extra/`)

Previously 20. Now **61** total.

- `test_governor_more_edges.py` (11): locale comma thousands/decimals, dot decimals, scientific notation, inverted/negative/missing meminfo, `disk_usage` OSError mid-stat and after parent walk, zero total, out-of-range reader does not crash `target()`.
- `test_mcp_stdio_more_edges.py` (15): chunked partial frames, last line without newline, CRLF/blanks, UTF-8 BOM, invalid UTF-8 then ping, notifications (no reply), duplicate ids, 20 interleaved requests, 40-item batch, non-object batch items, top-level scalar, 2000-deep nested params, non-string method, missing `HOME`, 8 rapid connect/shutdown cycles.
- `test_sovereign_edges.py` (6): SIGPIPE child, missing `HOME`, read-only cwd (echo ok, write fails as a result), non-UTF-8 stdout replacement, stdin=DEVNULL so `cat` cannot block, closed-stdout write is a result.
- `test_recon_edges.py` (9): loopback/localhost refused even when authorized; missing/`false` authorized refused; default-deny without allowlist; ports 0/65536, non-list, non-integer; authorized + 5 ports with mocked sockets (`open: none`).

## Pytest summaries (this round)

Full non-integration gate:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p no:randomly -m 'not integration' --timeout=120
3394 passed, 362 deselected, 1 warning in 57.48s
```

Shuffled (pytest-randomly auto-loaded):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration' --timeout=120
3394 passed, 362 deselected, 1 warning in 50.77s
```

Edge tests only:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p no:randomly tests/edge_extra --timeout=60
61 passed in 4.32s
```

0 failed, 0 errored, 0 skipped. 362 deselected are the project's `integration` marker. The earlier 3353 baseline plus 41 new edge tests is 3394.

## git diff --stat (uncommitted, branch `fix/macos-and-edge`)

```
 avrone/evolve.py          | 11 +++++-
 avrone/governor.py        | 73 ++++++++++++++++++++++++++++++-----
 avrone/mcp_server.py      | 96 ++++++++++++++++++++++++++++++++++++++---------
 avrone/sovereign.py       | 17 ++++++---
 tests/test_mcp_server.py  | 10 +++--
 tests/test_recon_extra.py |  3 ++
 6 files changed, 172 insertions(+), 38 deletions(-)
```

New this round (untracked):

```
tests/edge_extra/test_governor_more_edges.py
tests/edge_extra/test_mcp_stdio_more_edges.py
tests/edge_extra/test_recon_edges.py
tests/edge_extra/test_sovereign_edges.py
```

Nothing was pushed. Nothing was merged to `main`. The user still needs to name the push target before any GitHub write.
