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
