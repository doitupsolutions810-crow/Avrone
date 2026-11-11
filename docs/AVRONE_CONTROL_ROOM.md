# AVRONE Control Room

## Entry point

When AVRONE is running, open `/static/control-room.html`. The existing root application links to this page. The page is read-only and uses two evidence sources:

- Public GitHub REST endpoints for repository pull requests, commits, workflow runs, job summaries, artifact metadata, exact-SHA commit statuses, and deployment contexts.
- Same-origin AVRONE endpoints for status, Eternal CodeX runs, run replay, checkpoints, telemetry, fleet directory, and accepted evolution proposals.

The page does not require a GitHub token for the public repository. Some local AVRONE endpoints require the administrator token. The token is held in page memory only; it is not stored in localStorage, sessionStorage, cookies, or URLs.

## Evidence rules

GitHub lifecycle (OPEN/CLOSED, DRAFT/READY, MERGED/UNMERGED) is separate from evidence state (VERIFIED, UNVERIFIED, BLOCKED, NOT RUN). Check state (PASS/FAIL/SKIPPED) is separate again. All evidence must match the selected commit SHA. The Python matrix binds only to the repository's `CI` workflow (`.github/workflows/ci.yml`); a notification, deployment-comment, or other workflow on the same commit cannot mask or impersonate the CI result.

The repository CI workflow now emits evidence from four exact-SHA lanes: Python 3.11/3.12, Docker image verification, and Node 22 container lifecycle. Each Python leg validates and uploads `coverage-xml-py3.11` / `coverage-xml-py3.12` and `coverage-json-py3.11` / `coverage-json-py3.12`. The Docker job builds the repository Dockerfile, verifies the configured non-root user and healthcheck, runs the service, and records API/health/image inspect evidence as `docker-image-evidence`. The Node container job runs process termination, child reaping, timeout and exit-code tests in an isolated `node:22-slim` container and uploads `node-container-lifecycle-evidence`.

A successful Python CI matrix is necessary but not sufficient for the Control Room's full release gate. The Vercel commit-status context is shown separately; an error is FAIL, pending is UNVERIFIED, and success is still PENDING REVIEW until a preview URL is available and a real browser smoke test runs. A failed evidence API request is BLOCKED; a successful request with no matching run is NOT RUN; an expired artifact is EXPIRED. Coverage artifacts, Docker image verification, in-container Node lifecycle evidence, local Apple-Silicon/Ollama execution, and proposal/edge-case validation require exact-SHA evidence. Artifact names and successful job names alone are never sufficient.

## Current observed AVRONE API surfaces

- `GET /api/status`: runner, workspace, provider/model, squad limits, active agents, boot identifier.
- `GET /api/runs` and `GET /api/runs/{run_id}`: durable run summaries and event replay.
- `GET /api/checkpoints` and `GET /api/checkpoints/{id}`: resumable checkpoint state (admin).
- `GET /api/telemetry`: run/agent/error/fleet telemetry (local/admin).
- `GET /api/fleet/directory`: remote node, device, runner and agent presence (admin).
- `GET /api/evolve`: accepted evolution proposals (local/admin).

The UI intentionally does not call mutation endpoints. GitHub pagination is bounded and follows only HTTPS `api.github.com` links under this exact repository path. External navigation is restricted to the current HTTPS origin and official GitHub/Vercel hosts. Remote API content is escaped before rendering.

## Limits and known gaps

The current server API does not expose every desired field. Container CPU/memory quotas, per-agent lease owner/expiry, lane scheduler decisions, duplicate-lane rejection receipts, Node container lifecycle results, and proposal validation artifacts are shown as unavailable until a real API or exact-SHA artifact provides them. No synthetic record is passed off as production evidence.

GitHub Actions artifact metadata can identify candidate artifacts, but operators should inspect the linked artifact contents and provenance before satisfying the gate. A named or expired artifact alone is not proof that its contents are valid. The Node client test suite executes the actual extracted dashboard helpers with mocked HTTP responses, covering state semantics, SHA binding, CI-workflow identity, link safety, same-host and cross-host pagination abuse, malformed responses, rate limits, network failure, expired artifacts, and blocked-versus-missing evidence. The Python suite separately checks that the assets are served and connected to the expected API surfaces. The Python suite separately checks that the assets are served and connected to the expected API surfaces.

## Local browser automation

The Vercel `agent-browser` discovery skill is included at `.agents/skills/agent-browser/SKILL.md`. This is the project skill descriptor; it does not install the native CLI or download Chrome. On the authorized developer machine, install the skill with `npx skills add https://github.com/vercel-labs/agent-browser --skill agent-browser`. Then install the CLI with `npm install -g agent-browser` and `agent-browser install`, and run `agent-browser skills get core` before any browser automation. The published CLI requires Node.js 22 or later. The repository descriptor is the official upstream discovery stub; it does not install the CLI or Chrome.

Upstream source: https://github.com/vercel-labs/agent-browser

## Extended adversarial verification

The browser-client suite now contains 59 executable Node tests. The additional cases exercise credential-bearing navigation and pagination URLs, repeated pagination links, malformed successful API envelopes, request aborts and timer cleanup, cyclic payloads, throwing selector getters, null rows, duplicate CI-job identities, duplicate artifact identities, invalid timestamps, and malformed evidence collections. The release-state model must fail closed: malformed evidence is blocked or remains unverified, never promoted to PASS.

Workflow-detail responses are accepted only when the expected `jobs` or `artifacts` property is an array. A successful HTTP response with a missing or malformed list is recorded as blocked evidence. A valid empty array remains a legitimate empty response. Duplicate named evidence rows are ambiguous and cannot satisfy a gate.

These tests supplement (rather than replace) the Python contract/security suite, exact-SHA CI matrix, Docker verification, and isolated Node 22 lifecycle checks. A green result remains specific to the exact commit SHA that ran it.

### Runtime and list-boundary edge cases

GitHub list pages are rejected when a row is null, primitive, or an array; pagination timeouts remain active through response-body parsing. Local runtime lists are normalized before rendering, and rejected checkpoint, run, agent, proposal, or fleet records are disclosed rather than causing a renderer exception. The dashboard's local admin token is not forwarded to GitHub API requests.

### Additional concurrency, pagination, and HTML-injection cases

The client caps pagination at ten pages even when a larger limit is requested, rejects malformed `rel=next` headers instead of silently truncating, and accepts quoted or unquoted valid next relations. Pull-request identifiers are HTML-escaped at render time. Asynchronous commit-status responses are bound to the currently selected PR SHA so an older response cannot overwrite a newer selection. Refresh requests use generation IDs so an earlier overlapping refresh cannot replace the latest snapshot. Malformed list rows are filtered and disclosed, and JSON request deadlines remain active through response-body reads.
