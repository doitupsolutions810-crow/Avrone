# AVRONE Control Room

## Entry point

When AVRONE is running, open `/static/control-room.html`. The existing root application links to this page. The page is read-only and uses two evidence sources:

- Public GitHub REST endpoints for repository pull requests, commits, workflow runs, job summaries, and artifact metadata.
- Same-origin AVRONE endpoints for status, Eternal CodeX runs, run replay, checkpoints, telemetry, fleet directory, and accepted evolution proposals.

The page does not require a GitHub token for the public repository. Some local AVRONE endpoints require the administrator token. The token is held in page memory only; it is not stored in localStorage, sessionStorage, cookies, or URLs.

## Evidence rules

GitHub lifecycle (OPEN/CLOSED, DRAFT/READY, MERGED/UNMERGED) is separate from evidence state (VERIFIED, UNVERIFIED, BLOCKED, NOT RUN). Check state (PASS/FAIL/SKIPPED) is separate again. All evidence must match the selected commit SHA.

A successful Python CI matrix is necessary but not sufficient for the Control Room's full release gate. Coverage artifacts, Docker image verification, in-container Node lifecycle evidence, local Apple-Silicon/Ollama execution, and proposal/edge-case validation remain NOT RUN unless their exact-SHA evidence is actually observed. A missing runtime endpoint is BLOCKED/NOT CONNECTED, never PASS.

## Current observed AVRONE API surfaces

- `GET /api/status`: runner, workspace, provider/model, squad limits, active agents, boot identifier.
- `GET /api/runs` and `GET /api/runs/{run_id}`: durable run summaries and event replay.
- `GET /api/checkpoints` and `GET /api/checkpoints/{id}`: resumable checkpoint state (admin).
- `GET /api/telemetry`: run/agent/error/fleet telemetry (local/admin).
- `GET /api/fleet/directory`: remote node, device, runner and agent presence (admin).
- `GET /api/evolve`: accepted evolution proposals (local/admin).

The UI intentionally does not call mutation endpoints.

## Limits and known gaps

The current server API does not expose every desired field. Container CPU/memory quotas, per-agent lease owner/expiry, lane scheduler decisions, duplicate-lane rejection receipts, Node container lifecycle results, and proposal validation artifacts are shown as unavailable until a real API or exact-SHA artifact provides them. No synthetic record is passed off as production evidence.

GitHub Actions artifact metadata can identify candidate artifacts, but operators should inspect the linked artifact contents and provenance before satisfying the gate. A named artifact alone is not proof that its contents are valid.

## Local browser automation

The Vercel `agent-browser` discovery skill is included at `.agents/skills/agent-browser/SKILL.md`. This is the project skill descriptor; it does not install the native CLI or download Chrome. On the authorized developer machine, install with the upstream instructions and then run `agent-browser skills get core` before any browser automation.

Upstream source: https://github.com/vercel-labs/agent-browser
