# CYCLE12_BOOT — Broker Bypass

## Discovery
Six hours on GitHub's broker assumed it was the only scheduler. swarm-vm-runner's emergent-compile branch (a5877b31, 60 files) contains a complete internal execution plane:

- **coordinator/server.js** — HTTP on port 4100, tenant control12. Swarm jobs (/api/jobs) run a four-role chain: debugger, coder, tester, governance. Runner jobs (/api/runner/jobs → /claim → /complete) for the internal runner. /health reports redis.ok.
- **internal-runner/runner.mjs** — polls COORDINATOR_URL, claims runner jobs, executes allowlisted kinds: prove-success, health-probe, inventory, search, spawn-subagent. Walks local filesystem, can spawn subagents by POSTing objectives.
- **worker/worker.js** — four ROLE instances consuming swarm jobs from redis lists.
- **coordinator/redis-lite.js** — 302 bytes; degrades on missing redis instead of crashing.
- **content/serve.js** — artifact server on 8088.

## Canonical Boot (one command, from swarm-vm-runner root)
```bash
./scripts/launch-internal.sh
```
Starts coordinator, four workers, internal runner, content server — skips anything already alive, exits 1 if coordinator health fails.

## Full Stack
```bash
docker compose up -d
```
redis:7-alpine hard-gated before coordinator. Profiles: content, runner (GitHub actions-runner), vm.

## Attestation
```bash
./scripts/prove-success.sh
```
Posts objective, awaits role-chain completion, reports PASS/FAIL/DEGRADED. Hosted Actions explicitly optional.

## The Inversion
GitHub's frozen queue is a legacy ledger. The internal plane executes without the websocket. lineage_wire.py can now cross-examine coordinator logs against GitHub refusals — same system, two brokers, one truth.

## Sandbox Digest (unchanged)
fcf2f799 — three identical isolated runs, all green. Transient ok=false was a format-string glitch, not drift.
