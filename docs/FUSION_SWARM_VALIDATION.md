# Fusion and Swarm extension — November 3, 2026

Starting AVRONE checkpoint: `4232b83ece8662fa7c28a4cd59e660f3d203261a`. The prior unfiltered baseline was 3,868 passed and seven explicit capability skips. New results below are separate selections until the next full run.

## Complete repository refresh

Reviewed 40 accessible repositories, 367 branch heads, 286 open/closed PR records, 478 recent default-branch commits and 39 nonempty root inventories. Branches and PRs were paginated to exhaustion; commits were limited to 30 per repository. The empty repository returned GitHub 409 and is recorded as empty rather than omitted. The public catalog includes 18 repositories.

Google Drive's latest daemon checkpoint is round 40, matching `avrone-daemon` revision `13726f4887fbc692571ca78daa1fe578929953a3`. Neither connected Replit account returned an AVRONE/Grok/Fusion app; no new app or deployment is claimed. Pushes continue on the established AVRONE integration branch.

## Blackboard connection and durability

Adapted the blackboard proposal from AVRONE PR #20, revision `8cca4083eb94da32240a17a5a7ce87db519cdf7b`, retaining current Swarm operator authentication and queue fencing. GET/POST `/api/blackboard` now expose the existing run bridges under operator authentication. Bodies are capped at 4,096 bytes before authentication. A per-process boot ID supports peer restart detection; previous-token rotation is scoped to the blackboard.

The initial imported proposal passed 170 existing/contract cases. Eight new adversarial cases then failed: duplicate/non-finite JSON, surrogate Unicode, a newline in an ID, stale ordering across separate cores, and an uncaught order-publication failure. The hardening now uses strict UTF-8/finite JSON, complete ID matching, a cross-process sidecar lock, reloaded run journals and an fsynced atomic order file. HTTP writes require journal persistence; storage failures return 503 and an exact-ID retry repairs publication. An implementation lock-initialization error was caught and corrected locally before publication.

Final blackboard/bridge/server selection: **180 passed** in **5.73 seconds**. It includes cached-run refresh across cores, unchanged bridge chaos tests, authentication, request limits, deduplication and journal/index failure recovery. Reproduce with `python -m pytest -q tests/test_blackboard_api.py tests/test_blackboard_recovery.py tests/test_tool_env_report.py tests/test_bridge.py tests/test_bridge_chaos.py tests/test_swarm_concurrency.py tests/test_server_edges.py`.

## Fusion relay recovery

The Fusion Bridge main revision `3f8cc12a965486d5157431c7d906f04024b904cf` contains literal `PLACEHOLDER` in its README, relay and Great Scale evidence module. A local checkout reproduced a collection failure (`NameError`) when importing the relay. The pinned PR #1 implementation at `8c5ea85e973c52631cd9fc1b14e4392f19a1f14c` passes **35 tests** in **16.027 seconds** in a separate workspace. Those tests exercise explicit contract stubs; actual cross-repository HTTP execution is a separate next gate.

The Registry hashes adopted resources and this report. Live inference, Docker deployment, the physical Mac and unmerged proposals are not certified by these software tests.

## Signed Swarm replay and shared dispatch ownership

Adapted the reviewed PR #19 signing/outbox changes while retaining the existing protected key-file loaders, operator API and per-message thread locks. The active-tree selection passes **122 tests** in **1.42 seconds**. A timed-out Authority request now preserves and verifies its exact signed assertion/JTI. A persisted dispatch lease fences independent connections and refuses a result/rejection from an expired owner. The test fixtures now explicitly acquire ownership before recording receipts; signature-validation and stale-owner tests remain active.

This validates the producer and durable queue boundary. The Trinity routing proposal and actual deployed Authority/worker execution remain distinct integration gates.

## Actual HTTP and WebSocket execution

The recovered relay now completes **50 cycles and 21 assertions** against actual AVRONE, CycleKernal CognitiveBridge and CycleKernal WS Relay processes on loopback. An explicit metric frame crosses the real WebSocket; a non-finite frame is rejected without losing the subscription. An AVRONE finding reaches CycleKernal once, mood/health return to AVRONE, the canonical Great Scale snapshot retains its payload, retries deduplicate, and both services restart over durable data without echo loops. AVRONE's model mode is explicitly mock; these are transport and persistence checks.

This network run exposed a TCP reset hidden by in-process test clients: forced `Connection: close` could truncate an early 413 response while a client was still sending. The guard now lets the HTTP server drain unread frames. Declared and chunked bodies remain bounded and refused before authentication. The focused AVRONE selection, including repeated real TCP requests and connection recovery, passes **97 tests** in **4.34 seconds**. CycleKernal receives the corresponding repair separately; the network run used its pinned PR #14 source plus that one-line transport change.

## Trinity / API / CLI Swarm wiring

The shared submission boundary now routes `squad(..., dispatch="swarm")`, `/api/chat` and the CLI through the signed durable enqueue path. The focused selection passes **259 tests** in **3.66 seconds**. New cases exercise real SQLite state, delegation timeout followed by restart without a parent reload, task/message/correlation binding, competing connections with different candidate envelopes, exact signed replay, serialized legacy migration, rehashed non-finite/Unicode corruption, key-file alias protection, checkpoint identity, CLI forwarding and all public-chat policies. Explicit contract doubles represent the Authority/receiver; separate Ed25519 tests retain cryptographic verification.

The Authority assertion is persisted before I/O and kept unchanged after a timeout. Existing enqueues bypass redelegation. Public chat never grants access to the signer; Swarm dispatch requires the current operator token. Privileged tasks are refused before opening signing configuration. Queue/pending events do not emit `run.done`, and deep mode cannot treat a queued first wave as an answer. Conflicting identity/task reuse and audience changes fail closed. See [SWARM_DISPATCH.md](SWARM_DISPATCH.md) for configuration and retry semantics.

The normal local/network/hybrid paths remain covered by the existing checkpoint/netsquad tests. Actual deployed Authority issuance and specialist completion are still unverified; the runtime has no supplied live Swarm credentials. The next unfiltered regression supersedes these focused counts for the final tree.
