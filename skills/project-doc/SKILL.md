---
name: project-doc
description: Keep AVRONE versioning, release notes, verification status, safety boundaries, and daily engineering memory synchronized.
---

# AVRONE project documentation protocol

After each meaningful version change:

1. Update `pyproject.toml` and `avrone/__init__.py` when the package version changes; add a release section to `README.md`.
2. Record security changes, compatibility impact, tests added, known limitations, and exact verification status. Never report a test as passing unless it was executed and the result observed.
3. Keep this inventory aligned with the repository architecture and current version.
4. Append a dated entry to `memory/daily.md` with the tested commit or branch and any unavailable test/build environment.
5. Keep recon and OSINT bounded to one named domain/host, gated by `AVRONE_RECON` and the optional allowlist, with all calls logged. Never extend these tools toward deanonymizing, locating, or profiling private people.
6. Keep unit tests offline by patching HTTP and socket/open-connection calls. Mark real-uvicorn/network tests with `pytest.mark.integration`.

## Current architecture

- FastAPI server and mobile-friendly static HUD: `avrone/server.py`, `avrone/static/index.html`.
- Planner, parallel squad workers and Lead merge: `avrone/trinity.py`.
- OpenAI-compatible gateway with provider failover/circuit breakers: `avrone/gateway.py`, `avrone/llm.py`.
- Tool registry, extensions, plugins and MCP client/server: `avrone/tools.py`, `avrone/tools_extra.py`, `avrone/plugins.py`, `avrone/mcp_client.py`, `avrone/mcp_server.py`.
- Isolated self-evolution proposals: `avrone/evolve.py`.
- Bounded authorized infrastructure recon and public-domain OSINT aggregation: `avrone/recon.py`, `avrone/osint.py`.
- HMAC-signed fleet, durable jobs, presence, telemetry and event logs: `avrone/fleet.py`, `avrone/jobqueue.py`, `avrone/presence.py`, `avrone/telemetry.py`, `avrone/codex.py`.

## Historical v0.12.0 hardening baseline

The main v0.12.0 hardening commit fixes DNS-label allowlist matching, validates and de-duplicates scan ports, clamps MCP squad size to the published schema, and adds offline tests for CLI, MCP, legacy LLM and recon. The follow-on edge round makes OSINT log aggregation tolerant of malformed event lines, strengthens MCP input validation, adds fleet boundary/status tests, and configures the source ZIP artifact. Validation status must remain pending until a test gate completes successfully.

## v0.13 full-squad round

- Coverage lane: gateway fallback parsing/permanent failover/malformed-response edges and presence cap/route/ambiguity/render behavior.
- New deterministic tools: bounded unified text diff and strict JSON shape summary; both are registered as built-ins and have input/output limits.
- New skills: code review/regression analysis and release verification; both are discovered by the existing skill loader.
- Robustness fixes: ignore malformed/non-object Codex log records and avoid crashes in run listing/replay; validate JobQueue kind/payload/attempt limits and report a missing job id without mutating records.
- CI now requests a package-wide term-missing coverage report through pytest-cov.
- Validation must stay pending until the exact candidate commit executes the full suite on an assigned runner. A workflow configuration or runnerless failure is not a passing test.

## v0.13.1 self-evolution containment

- Validate proposal IDs before reading/deleting metadata, derive worktree paths from the validated ID, and never trust a worktree path from mutable JSON metadata.
- Resolve each requested file path and verify it is relative to the canonical worktree root. Reject absolute paths, parent traversal, .git metadata, duplicate paths, non-string content and over-limit edit payloads.
- Reject shell operators in the agent-supplied test command; only direct pytest invocations are accepted.
- Apply one monotonic deadline to setup, edits, testing, retry and commit. Record elapsed seconds and the enforced ceiling in each proposal event and metadata record.
- Never claim a change passed before both the test gate and proposal commit succeed. A timed-out or malformed proposal is rejected and its failure is recorded.

## v0.14 secure dispatch and controlled hosting

- The FleetManager request boundary is backed by avrone/dispatch.py: strict payload types, bounded route and prompt sizes, unique hops, explicit run modes, HTTPS-by-default peer URLs, and bounded node/replay state.
- Remote dashboard, run-log, telemetry, tools, gateway, evolution and event-stream surfaces check X-API-Token or the WebSocket token/cookie. Set AVRONE_API_AUTH=always behind reverse proxies; auto mode trusts direct loopback clients only.
- Runner subprocesses remove application/provider/cloud secrets from inherited environment. Keep local shell agents unprivileged; effective host root is denied unless the explicit break-glass override is enabled.
- Agent-directed fetch and generic HTTP requests share avrone/network_policy.py: strict http(s) URLs, no embedded credentials, block local/private/link-local/metadata destinations, DNS answer checks, redirect revalidation, cross-origin credential stripping, write-redirect denial, and a 4 MB decoded body limit. Document DNS rebinding as a residual that requires host-level egress controls.
- Fleet registry JSON must be symlink-safe and atomically replaced. Validate and cap persisted node IDs, URL/last-seen/load, capabilities, child lists, presence payloads and total registry count when loading a saved registry.
- The operator-only host gateway exposes only allowlisted service_status/service_restart operations and requires admin and separate root-operation tokens, plus OS-level least-privilege policy. Never describe this as arbitrary agent root access.
- Self-evolution reserves proposal duration and started-proposal counts in a cross-process UTC-day budget. Defaults: 900 seconds per proposal, 3600 seconds/day, 12 attempts/day. Hard ceilings: 1800 seconds/proposal, 14400 seconds/day and 100 attempts/day.
- Keep test and release status pending until the exact head executes the full suite and release-artifact build successfully on an assigned CI runner.
