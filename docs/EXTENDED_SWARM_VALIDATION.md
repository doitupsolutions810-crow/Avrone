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
