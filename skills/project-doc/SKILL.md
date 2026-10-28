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

## Current release: v0.12.0

The main v0.12.0 hardening commit fixes DNS-label allowlist matching, validates and de-duplicates scan ports, clamps MCP squad size to the published schema, and adds offline tests for CLI, MCP, legacy LLM and recon. The follow-on edge round makes OSINT log aggregation tolerant of malformed event lines, strengthens MCP input validation, adds fleet boundary/status tests, and configures the source ZIP artifact. Validation status must remain pending until a test gate completes successfully.
