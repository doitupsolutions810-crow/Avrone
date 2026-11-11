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

---

# Round 3 — harder protocol, framing, overflow, and Codex edges (2026-11-10 18:58 America/Detroit / EST)

Base: `fix/macos-and-edge` at `3fd5a743c95156fe74f0873596f582c1b6d0dd25` (Avrone main tip `5cceb52` plus the Round 1–2 fixes). Work stays on a fresh branch `edge/round-3` off that tip. The previous 61 `tests/edge_extra` cases were left intact; this round adds 60 genuinely new tests (121 total in the suite). Production recon guardrails were not weakened (port cap stays 32; 127.0.0.1 and all non-public addresses remain refused even with `authorized=True`; successful mocked scans still use ≤5 ports and never open a real remote socket).

## New tests (60)

| File | Count | What they cover |
|---|---|---|
| `tests/edge_extra/test_mcp_protocol_edges.py` | 19 | Mixed request+notification JSON-RPC batch; `id` as null/float/bool; `params` as array; unknown ping params; empty/unknown method names; `tools/call` arguments as array; Content-Length header vs newline framing; truncated multibyte UTF-8 split across chunks; embedded NUL; trailing garbage after valid JSON; empty/whitespace-only lines; just-under 16 MiB line; interleaved partial frames from two logical writers; shutdown while 3 `tools/call` are in flight; simultaneous mixed valid/invalid tool ids; `send()` EPIPE does not kill `serve()`; non-finite tool result does not crash the stream |
| `tests/edge_extra/test_governor_overflow_edges.py` | 8 | Combined thousands+decimal meminfo (`1,000.5`); `1e400` / `nan` MemTotal; uint64-boundary kB figures; disk free > total, negative free, 2^64 totals; `Governor.target()` with inf/NaN sensors stays in `[1, max_parallel]` |
| `tests/edge_extra/test_sovereign_more_edges.py` | 5 | Workspace removed after `start()`; child that rmdirs its own cwd mid-run; env value with an embedded NUL; missing `PATH`; `_reap()` of a `wait()` that hangs is bounded by the 2s cap |
| `tests/edge_extra/test_recon_reserved_edges.py` | 24 | `t_port_scan(..., authorized=True)` refuses IPv6 loopback (`::1`, `[::1]`), link-local (`fe80::1`, `169.254.1.1`), multicast (`ff02::1`, `224.0.0.1`, `239.255.255.250`), reserved (`240.0.0.1`, `0.0.0.0`, `255.255.255.255`, `198.18.0.1`); the same ranges refused when a public-looking name resolves to them (including mixed public+multicast); mocked happy path stays at ≤5 ports on `93.184.216.34`; default-deny holds with no allowlist |
| `tests/edge_extra/test_codex_edges.py` | 4 | `emit` sanitizes NaN/inf and lone surrogates so the JSONL is strict UTF-8 JSON; `emit("../..")` cannot write outside `log_dir`; `replay`/`runs` sanitize a legacy poisoned log so the API can re-encode it; slash `run_id` does not create subdirectories |

## Bugs found and fixed (root cause, Linux + macOS)

1. **`avrone/governor.py:73-77`** — `_read_mem_pct_proc` accepted `float("1e400")` → `inf`. Then `100 * (1 - finite/inf)` became `100.0`, so a corrupt meminfo was treated as 100% used and silently throttled the squad. Non-finite values are now skipped (same 0..100 clamp still applies afterward).
2. **`avrone/governor.py:150-156`** — `make_disk_reader` returned `100.0 * free / total` with no range check, so a mocked/corrupt `disk_usage` with `free > total` or a negative free would feed an out-of-range percent into the governor. Now returns `None` unless the percent is in `0..100`, and guards the division.
3. **`avrone/mcp_server.py:146-161`** — `AvroneMCPServer.send()` used `json.dumps` without `allow_nan=False` and wrote to the client with no pipe error handling. A closed client (`BrokenPipeError` / `OSError`) escaped `serve()` and killed the stdio loop; a NaN/surrogate in a tool result could also crash the encode. `send()` now dumps with `allow_nan=False` (falling back to a `-32603` JSON-RPC error if the payload is unencodable) and swallows `BrokenPipeError`/`ConnectionResetError`/`OSError` on write/flush.
4. **`avrone/codex.py:31-50`, `179-196`, `233`, `253`** — `Codex.emit` wrote `json.dumps(..., default=str)` (NaN/Infinity legal) and joined `log_dir / f"{run_id}.jsonl"` with no confinement, so `run_id="../codex-escape"` wrote *outside* the log directory. `replay`/`runs` of a legacy NaN/surrogate log then failed `json.dumps(..., allow_nan=False)` on the API path. Added `_json_safe` (reuses `gateway.clean_text` for lone surrogates/NULs; non-finite floats become `None`), dump with `allow_nan=False`, and only write when `path.resolve().is_relative_to(log_dir.resolve())`. `OSError` on write is swallowed so a missing parent dir cannot crash `emit`. `replay`/`runs` also run `_json_safe` on returned records.

No production recon policy was changed. No syntax errors found beyond the defects above.

## Gates (0 failed, 0 errored)

Full non-integration:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration'
3454 passed, 362 deselected, 1 warning in 57.64s
```

Shuffled (pytest-randomly):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p randomly -m 'not integration'
3454 passed, 362 deselected, 1 warning in 64.63s
```

Edge suite (61 previous + 60 new = 121):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider tests/edge_extra
121 passed in 8.99s
```

3454 = previous 3394 + 60 new tests. The warning is the existing Starlette/httpx `TestClient` deprecation. 362 deselected are the project's `integration` marker. 0 skipped.

Round 2 was already pushed as `fix/macos-and-edge` @ `3fd5a74`. This round is committed and pushed only on `edge/round-3`. `main` is left at `5cceb52`. No PR opened.

---

# Round 4 — HUD, mcp_client, config, plugins, trinity, evolve (2026-11-10 19:06 America/Detroit / EST)

Base: `edge/round-3` at `c37aba76175c717ba0c26aa8e44994ddf2988539` (Avrone main tip `5cceb52` plus Rounds 1–3). Work is on a fresh branch `edge/round-4` off that tip. The previous 121 `tests/edge_extra` cases were left intact; this round adds 37 genuinely new tests (158 total in the suite) in areas those rounds touched less: the FastAPI HUD, `mcp_client`, config/bootstrap, plugins, trinity history compaction, and evolve gate-command resolution. Production recon guardrails were not changed.

## New tests (37)

| File | Count | What they cover |
|---|---|---|
| `tests/edge_extra/test_server_hud_edges.py` | 14 | `/api/blackboard` with the wrong Content-Type, empty/non-UTF-8 bodies, emoji + unknown fields, surrogate keys, HEAD/OPTIONS/PUT/PATCH/DELETE, a 20k-char `since` query, traversal/NUL/emoji cursors, concurrent GET+POST; `/api/status`/`/api/tools` HEAD; `/api/telemetry` and `/api/map` with nan/inf/junk query values; checkpoint path traversal; `server.main()` junk/out-of-range `AVRONE_PORT`; telemetry/system_map non-finite windows |
| `tests/edge_extra/test_mcp_client_more_edges.py` | 7 | A 2000-deep JSON stdout line must not fail in-flight requests; partial then completed frames (slow drip); duplicate + unknown response ids; stdout EOF mid-request cleans `_pending`; `load_config` on a directory, a broken symlink, a symlink to a valid file, unknown keys / disabled servers |
| `tests/edge_extra/test_bootstrap_config_edges.py` | 5 | `_bool` strips whitespace (`" true "`, `"\tOFF\n"`); `Settings.workspace` follows a symlink; whitespace path does not crash; dotenv blank/export/spaced-key lines; `apply_tool_focus` with whitespace-only and duplicate names |
| `tests/edge_extra/test_plugins_more_edges.py` | 3 | `load_plugins` follows a `.py` symlink, skips a broken symlink, reports a binary `.py` without crashing, and loads when the plugins directory itself is a symlink |
| `tests/edge_extra/test_trinity_more_edges.py` | 4 | `compact_messages` with int/list content and non-dict items (used to TypeError); junk/NaN/inf/negative limits and a non-list history; `out_of_turns_result` on None/bytes/tool-call leftovers |
| `tests/edge_extra/test_evolve_cmd_edges.py` | 4 | `_default_test_cmd()` uses `shlex.quote(sys.executable)` (PATH regression guard); `AVRONE_EVOLVE_TEST_CMD` override; timeout `<= 0` clamped to 1; junk timeout falls back to 900; malformed edits return an error result |

Autopilot bash lanes (`deepcov`/`newmods`/`tools`/`robust`) already have integration coverage in `tests/test_autopilot_edges.py` (deselected by `-m 'not integration'`). This round guards the Python-side gate command and propose failure modes those lanes rely on.

## Bugs found and fixed (root cause, Linux + macOS)

1. **`avrone/trinity.py:483-509`** — `compact_messages` used `len(m.get("content") or "")` and `m.get("role")` without checking types. A history item that was not a dict, or whose `content` was an int/list, raised `TypeError` and could crash the agent loop. Non-list histories, non-finite/negative limits, and non-string content are now skipped; only string bodies count toward the size.
2. **`avrone/mcp_client.py:105-109`** — `StdioTransport._read_loop` only ignored `JSONDecodeError`. A deeply nested stdout line raised `RecursionError` (a `RuntimeError`/`Exception`), which jumped to the `finally` and failed *every* in-flight request with "MCP server process exited" even though the child was still running. `RecursionError`/`ValueError`/`TypeError` are now ignored like other poison lines.
3. **`avrone/mcp_client.py:431-438`** — `load_config` used `exists()` then `read_text()`, so a path that was a directory or a broken symlink raised `IsADirectoryError`/`OSError` to any caller that did not catch. Missing, non-file, unreadable, or undecodable paths now return `{}` (malformed JSON still raises, as the existing unit test requires).
4. **`avrone/config.py:159-160`** — `_bool` did not strip whitespace, so `AVRONE_GOVERNOR= true ` was silently `False` and disabled the governor. Values are now `.strip().lower()` before the truthy set is checked.
5. **`avrone/server.py:740-751`** — `main()` did `int(os.getenv("AVRONE_PORT", "8710"))`, so `AVRONE_PORT=8710abc` / `0` / `65536` crashed process startup. Junk or out-of-range values now fall back to 8710.
6. **`avrone/telemetry.py:35-40, 45, 144`** — `/api/telemetry?hours=inf` (and `system_map`) computed `time.time() - inf` and could emit non-finite `window_s` into a JSON response. `_finite_window` rejects non-finite, negative, and non-numeric windows and falls back to 86400s.
7. **`avrone/evolve.py:203-206`** — `_propose_once` did `int(timeout)` with no guard, so a junk timeout raised `ValueError` out of the propose path. Non-numeric values now fall back to 900s (and `<= 0` was already clamped to 1).

No production recon policy was changed.

## Gates (0 failed, 0 errored)

Full non-integration:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration'
3491 passed, 362 deselected, 1 warning in 61.43s
```

Shuffled (pytest-randomly):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p randomly -m 'not integration'
3491 passed, 362 deselected, 1 warning in 55.23s
```

Edge suite (121 previous + 37 new = 158):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider tests/edge_extra
158 passed in 8.28s
```

3491 = previous 3454 + 37 new tests. The warning is the existing Starlette/httpx `TestClient` deprecation. 362 deselected are the project's `integration` marker. 0 skipped.

This round is committed and pushed only on `edge/round-4`. `main` is left at `5cceb52`. No PR opened.

---

# Round 5 — governor runtime, sovereign signals, MCP e2e, evolve quoting, HUD lifespan (2026-11-10 19:28 America/Detroit / EST)

Base: `edge/round-4` at `b205efb1f4ebe5ea5141596c1902534082424d6c` (Avrone main tip `5cceb52` plus Rounds 1–4). Work is on a fresh branch `edge/round-5` off that tip. The previous 158 `tests/edge_extra` cases were left intact; this round adds 31 genuinely new tests (189 total). Production recon guardrails were not changed.

## New tests (31)

| File | Count | What they cover |
|---|---|---|
| `tests/edge_extra/test_governor_runtime_edges.py` | 7 | `from_settings` strips `AVRONE_GOVERNOR` whitespace; junk/`inf`/`nan` `want` does not raise; repeated `target()` honors TTL cache and increments `throttle_count`; custom/replaced sensor registry; `is_hot`/`state` while throttling is off; `NodeHealth.as_dict` rounding; 32 concurrent `target()` calls |
| `tests/edge_extra/test_sovereign_runtime_edges.py` | 8 | spaces/quotes/newlines in `bash -lc` commands; unicode in argv and output; stdout+stderr both captured; non-zero exit preserved; SIGTERM and SIGKILL are results; timeout+_reap leaves no zombie (SIGTERM ignored, SIGKILL reaps, elapsed < 8s); very large env is a CommandResult (E2BIG or success); argv with spaces stays one argument |
| `tests/edge_extra/test_mcp_jsonrpc_more_edges.py` | 6 | missing/`1.0`/numeric `jsonrpc` is -32600; empty-string and 10^50 ids are echoed; object/array/NaN ids are rejected with `id: null`; `tools/list` ignores `cursor` and has no `nextCursor`; a raising tool becomes `isError` not an RPC crash; 20 pings through a slow writer still complete (backpressure) |
| `tests/edge_extra/test_mcp_e2e_client_server.py` | 2 | Real `MCPClient` over a real `python -m avrone.mcp_server` pipe: initialize, list, ping, `avrone_tools` (many builtins), mock chat, unknown tool raises `MCPError`, clean `_pending`; a tool-level error still lets a later ping succeed |
| `tests/edge_extra/test_evolve_governor_interact.py` | 4 | `_safe_test_cmd` re-quotes `;` / `&&` so they are not operators; env override `true; echo INJECTED` under a throttling governor cannot inject; `_sh` quotes a worktree path that contains spaces; a 30s sleep honours timeout+2s reap cap |
| `tests/edge_extra/test_server_lifespan_edges.py` | 4 | `/api/status` ignores adversarial query params; `/api/tools` and `/api/evolve` stay stable; checkpoint GET/DELETE junk ids are not 5xx; TestClient lifespan start/stop/start works and boot id is process-stable |

## Bugs found and fixed (root cause, Linux + macOS)

1. **`avrone/governor.py:259-260`** — `Governor.from_settings` did not strip `AVRONE_GOVERNOR`, so `" off "` left the governor enabled. Values are now `.strip().lower()` before the off-set check (aligned with `config._bool`).
2. **`avrone/governor.py:316-328`** — `target(want=...)` did `int(want)` and `min(cap, want or cap)` *outside* the sensor `try`, so `want="nope"` / `inf` raised `TypeError`/`ValueError`/`OverflowError` out of `target()`. Junk wants are ignored; numeric wants still clamp the result.
3. **`avrone/mcp_server.py:125-137, 251-259`** — `_handle_safe` did not check `jsonrpc` or the id type. Missing/wrong versions and object/array/bool/NaN ids are now `-32600` (bad ids are answered with `id: null` so the error stays encodable). Empty strings and large ints remain valid ids.
4. **`avrone/evolve.py:79, 168-175, 213-218, 227, 232`** — `_sh` concatenated `cd {cwd}` unquoted (a space in the worktree broke the gate). `AVRONE_EVOLVE_TEST_CMD` was interpolated raw into `bash -lc`, so `true; echo INJECTED` ran a second command. Cwd and `PYTHONPATH` worktree are now `shlex.quote`d; env overrides go through `_safe_test_cmd` (explicit `test_cmd` arguments, including compound skill gates like `test -f && grep`, are left as-is).

No production recon policy was changed.

## Gates (0 failed, 0 errored)

Full non-integration:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration'
3522 passed, 362 deselected, 1 warning in 65.23s
```

Shuffled (pytest-randomly):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p randomly -m 'not integration'
3522 passed, 362 deselected, 1 warning in 67.42s
```

Edge suite (158 previous + 31 new = 189):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider tests/edge_extra
189 passed in 11.07s
```

3522 = previous 3491 + 31 new tests. The warning is the existing Starlette/httpx `TestClient` deprecation. 362 deselected are the project's `integration` marker. 0 skipped.

This round is committed and pushed only on `edge/round-5`. `main` is left at `5cceb52`. No PR opened.

---

# Round 6 — property tests, MCP fuzz, remaining modules, mock autopilot tick (2026-11-10 19:43 America/Detroit / EST)

Base: `edge/round-5` at `cf5aee79eb0bce6edba3d9429081cff160d90c6e` (Avrone main tip `5cceb52` plus Rounds 1–5). Work is on a fresh branch `edge/round-6` off that tip. The previous 189 `tests/edge_extra` cases were left intact; this round adds 60 genuinely new tests (249 total). Production recon guardrails were not changed.

## Test dependency: hypothesis

Hypothesis was **not** already in the project. It is now a *dev/test* extra, not a runtime dependency:

- `pyproject.toml` `[project.optional-dependencies] dev` now includes `hypothesis>=6.100` (alongside pytest, pytest-asyncio, pillow).
- Installed into the local venv as **hypothesis 6.170.15**.
- Property tests use `@settings(max_examples=25–40, deadline=None, derandomize=True, database=None)` so the suite stays fast and deterministic under a shuffled pytest run.
- `.hypothesis/` (the local example database) was added to `.gitignore`.

Install for development with `pip install -e '.[dev]'`.

## New tests (60)

| File | Count | What they cover |
|---|---|---|
| `tests/edge_extra/test_hypothesis_parsers.py` | 19 | Property tests for `_json_safe` (always `allow_nan=False` encodable; non-finite floats → `None`; strings UTF-8 / NUL-free), `clean_text`, `parse_dotenv_line` (never raises; `KEY=VAL` keeps the key), `/proc/meminfo` and `make_disk_reader` (no crash, pct is `None` or 0..100), `_bool` / `replay_limit` / `_finite_window` clamps, `shlex.quote`/`split` round-trip, `_safe_test_cmd` always splitable (empty falls back to the default), `_ok_rpc_id` vs JSON-RPC scalar rules, `compact_messages` never raises and preserves length, `context_chars` / `tool_output_max` / `clamp_deep_rounds`, `iter_lines` drops overlong lines and keeps the rest |
| `tests/edge_extra/test_mcp_stdio_fuzz.py` | 11 | 8 seeded random byte-stream cases (garbage / NUL / truncated UTF-8 / whitespace / valid / batch / partial); a well-formed ping in a noisy stream is still answered; hypothesis-fuzzed chunk lists never hang or emit invalid JSON-RPC; a real `python -m avrone.mcp_server` subprocess fed a seeded fuzz stream exits under an 8s reap cap |
| `tests/edge_extra/test_module_gaps_r6.py` | 24 | Governor: sensor exceptions / NaN become errors, typo keys and non-callables don't crash, all-`None` sensors return cap, `inf` load throttles to floor. Sovereign: parent-env inheritance without an agent cwd; `agent_env` sets `AVRONE_AGENT_DIR` and still inherits `PATH` and other parent vars. `StdioTransport`: close is idempotent; `connect()` twice replaces the child (no leak); close fails an in-flight request. Plugins: discovery is sorted by filename; same tool name → last sorted file wins; a file without `register` is skipped. Bootstrap: `apply_tool_focus` after registration; `build_core` cleanup is idempotent. Trinity: compact at the exact limit is a no-op; just-under elides a `TOOL RESULT` body but not the tail; 0 / empty / negative limits are no-ops. Codex: `iter_lines` at the exact cap; `replay` honors `HARD_REPLAY_EVENTS`. Telemetry windows stay JSON-safe and honor the horizon. 16-thread checkpoint `claim` race: exactly one winner, release clears busy |
| `tests/edge_extra/test_autopilot_tick_r6.py` | 6 | A full mock autopilot tick (governor → evolve gate command via LocalRunner → MCP ping) is healthy by default; injected faults at each stage (hot governor, sanitized evolve command with metacharacters, sovereign timeout/reap, truncated MCP frame) still shut down cleanly with no leaked asyncio tasks or leftover children |

## Bugs found and fixed (root cause, Linux + macOS)

1. **`avrone/mcp_client.py:79-86`** — `StdioTransport.connect()` always started a new child and reader tasks, even when `self.proc` was already set. A second `connect()` (reconnect, or a caller that connected twice) leaked the previous subprocess and its `_read_loop` / stderr-drain tasks. `connect()` now `close()`s an existing transport first and clears `proc`, reader tasks, and `_pending`.
2. **`avrone/telemetry.py:35-40`** — `_finite_window` is annotated `-> float` and used as a JSON field, but the fallback default was the bare int `86400`. Junk / `None` / non-finite inputs therefore returned an `int`. The default is now `86400.0` and both return paths use `float(default)` so the result is always a finite non-negative float.

No production recon policy was changed.

## Gates (0 failed, 0 errored)

Full non-integration:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration'
3582 passed, 362 deselected, 1 warning in 67.47s
```

Shuffled (pytest-randomly):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p randomly -m 'not integration'
3582 passed, 362 deselected, 1 warning in 70.87s
```

Edge suite (189 previous + 60 new = 249):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider tests/edge_extra
249 passed in 24.56s
```

3582 = previous 3522 + 60 new tests. The warning is the existing Starlette/httpx `TestClient` deprecation. 362 deselected are the project's `integration` marker. 0 skipped.

This round is committed and pushed only on `edge/round-6`. `main` is left at `5cceb52`. No PR opened.

---

# Round 7 — stateful properties, long-run stability, resource cleanup, error paths, Unicode (2026-11-10 19:54 America/Detroit / EST)

Base: `edge/round-6` at `91b626d3155e721b3e80b207d49ba58a50fa08ef` (Avrone main tip `5cceb52` plus Rounds 1–6). Work is on a fresh branch `edge/round-7` off that tip. The previous 249 `tests/edge_extra` cases were left intact; this round adds 42 genuinely new tests (291 total). Production recon guardrails were not changed. Hypothesis remains a dev/test extra (derandomized, `max_examples` 12–35, `stateful_step_count` 10, `database=None`).

## New tests (42)

| File | Count | What they cover |
|---|---|---|
| `tests/edge_extra/test_hypothesis_roundtrip_r7.py` | 8 | JSON-RPC and JSON-value encode→decode→encode stability; dotenv serialize→load; Settings int-env round-trip; `_num` never returns non-finite; argv list `quote`/`split` round-trip; blackboard write→read fidelity (including duplicate-id); a Hypothesis `RuleBasedStateMachine` of an MCP stdio session (requests, string ids, notifications, junk, bad version) with "exactly one well-formed response per pending id" |
| `tests/edge_extra/test_longrun_stability_r7.py` | 7 | Governor over 400 oscillating ticks: `throttle_count` monotonic and ≤ ticks, `readers`/`_cache` sizes stay constant, recovers from hot load; 200 steady-hot ticks do not grow internal state; evolve `loop_limits` / `hard_caps` / `long_loops_allowed` with junk env and cap overflow; `_checkpoint_candidates` drops oversized/non-encodable lists; `_safe_test_cmd` unclosed quote is a quoted fallback; 6 repeated reap-cap kills leave no `sleep` zombies; telemetry over 80 samples plus poison lines and inverted timestamps, percentiles non-negative and monotonic |
| `tests/edge_extra/test_resource_exhaustion_r7.py` | 5 | 16 concurrent in-process MCP sessions (ping + tools/list) with no leaked tasks; `avrone_tools` with 250 registered tools; ToolRegistry of 300 names stays complete; a 64 KiB JSON-RPC line is answered; 30 StdioTransport connect/close cycles do not leak fds (`/dev/fd` slack ≤ 8) or reader tasks |
| `tests/edge_extra/test_error_paths_r7.py` | 15 | `_clamp_size` / `_clamp_rounds` / `max_inflight` junk; `resolve_provider` unknown + `_num` junk; `_load_dotenv` missing/dir/unreadable; plugin syntax and `register()` errors are reports and do not block a good plugin; `load_config` directory / disabled / malformed JSON; MCPManager start failure is a status; illegal MCP tool names are skipped; sovereign missing binary is a result; cancelled `run` reaps the child; governor env/sensor/factors fallbacks; Codex emit after log_dir vanishes; telemetry `node_health`/`fleet_health` error fallbacks; `apply_tool_focus` empty/unknown; `run_cleanups` swallows one failure; `compact_messages` non-list / junk limit |
| `tests/edge_extra/test_unicode_i18n_r7.py` | 7 | RTL (Arabic/Hebrew), zero-width, combining-mark stacks, long grapheme clusters, family ZWJ emoji through `clean_text` / `_json_safe` / `_utf8_text`; ZW/RTL preserved (only NUL/surrogates are dropped); blackboard write/read; Codex emit/replay; dotenv values; argv quote/split lists; LocalRunner stdout capture |

## Bugs found and fixed (root cause, Linux + macOS)

1. **`avrone/telemetry.py:21-68, 118-124`** — `_iter_events` assumed every JSONL line was a `dict` with a numeric `ts`. A JSON array/number/string raised `AttributeError`; a string `ts` raised `TypeError` (`>=` against the window). Deeply nested JSON and unreadable files were also unhandled. `_event_ts` now accepts only dicts with a finite numeric timestamp; parse errors include `RecursionError`/`ValueError`/`TypeError`; missing/unreadable logs are skipped. Separately, a `run.done` whose `ts` precedes `run.start` produced **negative** `p50_s`/`p95_s`. Durations are now `max(0.0, end - start)` and ignore non-finite stamps.
2. **`avrone/evolve.py:293-302`** — `_env_num` treated `inf` as a valid positive limit (`float("inf") == inf` and `inf > 0` are both true), so `AVRONE_EVOLVE_TIME_BUDGET=inf` could disable a timeout if the value were used raw. Non-finite values (and non-positive) now return `None` and fall back to the documented caps.

No production recon policy was changed.

## Gates (0 failed, 0 errored)

Full non-integration:

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -m 'not integration'
3624 passed, 362 deselected, 1 warning in 79.87s
```

Shuffled (pytest-randomly):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider -p randomly -m 'not integration'
3624 passed, 362 deselected, 1 warning in 77.22s
```

Edge suite (249 previous + 42 new = 291):

```
AVRONE_LLM_MODE=mock python -m pytest -q -p no:cacheprovider tests/edge_extra
291 passed in 22.92s
```

3624 = previous 3582 + 42 new tests. The warning is the existing Starlette/httpx `TestClient` deprecation. 362 deselected are the project's `integration` marker. 0 skipped.

This round is committed and pushed only on `edge/round-7`. `main` is left at `5cceb52`. No PR opened.
