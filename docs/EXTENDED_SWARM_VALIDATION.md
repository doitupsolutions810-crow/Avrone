# Extended Swarm validation — November 3, 2026

Remote starting snapshot: `5c0488da4b07d5672411a1d115c6454287999027` on `integration/grokbot-unified-launch-20261102`. Its complete local Git tree matched `b28c3c354c92a733d471fda753e81d0fdbda7e64` before edits.

## Round 1: startup and lifecycle repair

The signed Swarm baseline had **19 failures and 47 passes**. Eighteen failures reproduced a real application import/startup error: FastAPI 0.162 no longer exposes `app.add_event_handler`. Registering a shutdown event would also omit cleanup when the parent application supplies its own lifespan. The remaining failure was an incorrectly constructed test client.

Swarm now composes with the existing runtime lifespan, retains its request-state dictionary, and closes its outbox and HTTP client before the parent runtime shuts down. A regression checks the startup/shutdown order and verifies that the database is closed.

After repair: **114 passed**, including signed outbox, Authority client, provisioning, API, and server edge cases, in 2.65 seconds on Python 3.12.14. The unchanged Node contracts passed **49/49** in 0.30 seconds. The earlier bridge/queue baseline at `123726fbc5868f6e8a2e0b1dc24882d4e628a5dd` passed **143/143** in 10.40 seconds. These are distinct selections, not a full-suite result.

Reproduce with `python -m pytest -q tests/test_swarm_outbox.py tests/test_swarm_api.py tests/test_swarm_authority_client.py tests/test_swarm_provisioning.py tests/test_server_edges.py` and `npm --prefix integrations/grok-bot test`.

The Registry now includes the signed Swarm resources, their exact content hashes and their starting revision. Six stale hashes introduced by the new Swarm wiring were refreshed.

Runtime reference: [FastAPI lifespan documentation](https://fastapi.tiangolo.com/advanced/events/). Dependency versions were read from the installed environment.

Protocol tests use explicit HTTP/model doubles where indicated. Docker, a physical Mac, and an actual model-backed deployment have not been exercised in this environment. Queue acceptance is not task completion.

## Round 2: recoverable, fenced queue execution

Adapted the daemon queue and its regression suite from AVRONE revision `267259656eae89ab6e2cda1d697afd7e90723c10` (round 29 queue work retained in the round 31 checkpoint). New boundary tests first produced **33 failures and 2 passes** against that implementation. They reproduced expired-owner writes, invalid UTF-8/non-finite/non-object payloads, escaping that exceeded result budgets, terminal-result replacement, abandoned in-flight claims, missing rollback after a failed COMMIT, lost-lease work continuing, and invalid fleet jobs reaching execution.

The integration now gives each attempt a fresh token, checks lease ownership inside the SQLite transaction, maintains heartbeats, cancels handlers after ownership loss, drains and releases a claim interrupted by shutdown, quarantines poison rows, and recovers the worker after claim/failure-storage errors. Result truncation preserves valid bounded UTF-8 JSON. Existing local tokenless callers remain compatible for running jobs; background workers always supply their claim token, and terminal jobs cannot be overwritten.

The fleet queue validates kind and text fields before dispatch, and a local deep job now invokes the deep runtime instead of silently running an ordinary squad.

Final queue/fleet selection: **163 passed** in 8.24 seconds, including the four-connection repeated claim race, old-schema migration, long jobs spanning multiple leases, cancellation during a real SQLite thread operation, Unicode/size cases and cross-connection stale-owner fencing.

Reproduce: `python -m pytest -q tests/test_queue_lease_edges.py tests/test_jobqueue_robust.py tests/test_queue_inbox.py tests/test_jobqueue_shutdown.py tests/test_fleet_edges.py tests/test_fleet_gateway.py tests/test_robust_v021_state.py::test_jobqueue_two_queues_on_one_file_never_double_claim`.

## Round 3: concurrent Swarm API and operator boundary

The first concurrency/API boundary run had **6 failures and 7 passes**. A blocked synchronous enqueue prevented an unrelated health endpoint from responding; a corrupted stored request escaped as an application exception; and the real server passed its public-chat access policy into the signing routes instead of requiring an operator token.

Swarm network and SQLite work now run in FastAPI's worker pool. Dispatcher initialization is serialized, and a per-message lock coalesces concurrent same-process submits/retries without retaining unused lock entries. The cross-instance test still sends two independent requests and verifies that both converge on the same persisted receipt; receiver-side signed idempotency remains necessary across processes. Every signing route now requires the operator token even if public chat is open or the caller is on loopback. Corrupt status records return HTTP 409.

After repair: **133 passed** in 3.36 seconds across the Swarm API/concurrency/outbox/Authority/provisioning tests and server/access-policy regressions. The suite includes eight simultaneous first requests, eight duplicate requests, a deliberately stalled send with a responsive health probe, preserved lifespan cleanup, and all three public-access policies.

Reproduce: `python -m pytest -q tests/test_swarm_concurrency.py tests/test_swarm_api.py tests/test_swarm_outbox.py tests/test_swarm_authority_client.py tests/test_swarm_provisioning.py tests/test_server_edges.py tests/test_robust_v027_access.py`.

## Round 4: complete suite and generated inventory

An unfiltered four-worker run finished with **3,867 passed, 7 skipped and 1 failed**, in **639.18 seconds**. The sole failure was `test_committed_prompt_is_current`: the committed generated prompt predated the signed Swarm modules and the new test files. No runtime test failed in that run.

Regenerated `MASTER_PROMPT.md` and corrected its generator so it asks operators to measure the current test/coverage results rather than advertising stale fixed pass/skip counts or a 99% coverage expectation. The generator also distinguishes an observed runner-ID-0/no-steps CI failure from an unverified explanation for that failure. Its two existing integrity tests pass after regeneration.

Measured statement coverage from the full run: **97.9257%**, or **13,030 / 13,306** statements. This includes the recently added signing client; the old 99.53% figure covered a smaller source tree and is not the current coverage figure.

Syntax/format admission passed for **273 Python files, 56 JavaScript files, 6 shell scripts and 8 JSON files**, plus the Node console's inline JavaScript block.

The refreshed repository survey covers **39 repositories, 366 branch heads, 286 PR records and 447 recent default-branch commits**. Branch/PR lists were paginated to exhaustion. Default-branch commit history remains limited to 30 per repository. Public resource pins and proposal decisions are recorded in `REPOSITORY_UPGRADE_REVIEW.json` and `UPGRADE_REVIEW.md`.

## Final unfiltered result

At source revision `30c62ea9a39b08ac2bb939f69655821ac837655b`, `python -m pytest -q -n 8` completed in **355.60 seconds** with **3,868 passed, 7 skipped, 0 failed**. No runtime source changed after the coverage run. The seven explicit skips are four unavailable PID/procfs visibility cases, two root-only permission limitations, and the optional barcode dependency.

A real uvicorn process served the application HTML and status over loopback HTTP, completed a deliberately mocked chat and persisted six run events, refused all three signing routes without operator credentials, and returned 503 when Swarm trust was absent. A separate real-HTTP council probe returned **409**, with `A council requires live inference; mock mode cannot produce a verified receipt`. It issued no verified receipt. This confirms the mock-mode gate and does not establish live inference.

Machine-readable counts, dependency versions, capability skips, evidence-file SHA-256 digests and revision pins are in [VALIDATION_RESULTS.json](VALIDATION_RESULTS.json). The unchanged Node contract selection passed **49/49**.

GitHub Actions run [41681381893](https://github.com/doitupsolutions810-crow/Avrone/actions/runs/41681381893) failed without executing test steps: all three jobs had runner ID 0 and zero steps. Local results and remote CI status are separate; no remote pass is claimed. Docker builds, actual model-backed council/Swarm execution, persistent browser operation and a physical Mac remain unverified. The unmerged Authority, Trinity and Fusion proposals in the repository review are not activated by this change.
