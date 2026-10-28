# AVRONE daily engineering memory

## 2026-10-28 — v0.12 edge-coverage follow-on

- Resumed from `doitupsolutions810-crow/Avrone`. The live `main` head advanced during review to `2f7cca8` (v0.12.0), containing the recon allowlist fix, port validation, MCP squad-size clamp/version report, and offline CLI/MCP/LLM/recon suites.
- Added OSINT aggregation hardening so malformed JSONL, JSON scalars/lists, non-string event kinds, malformed payloads, and non-string host values are skipped rather than crashing metrics.
- Added offline tests for OSINT aggregation edge records, MCP invalid parameter shapes/early task validation, and fleet online/degraded/offline freshness plus auth errors.
- Added a CI source-archive job: after the Python 3.11/3.12 matrix passes, upload `avrone.zip` as artifact `avrone-<commit-sha>` for 14 days.
- Verification caveat: shell cloning cannot resolve `github.com` in this execution environment. Existing CI runs show `runner_id=0` with no executed steps, so no green GitHub Actions result is available yet. The follow-on branch must not be merged until a real green test result is observed.

## 2026-10-28 — Full squad round 2 (v0.13 candidate)

- Four independent GitHub review lanes based on squad/v0.12-osint-metrics:
  - Coverage: squad/round2-coverage — gateway and presence edge suites.
  - Tooling: squad/round2-tools — bounded text_diff and json_inspect.
  - Skills: squad/round2-skills — code-review and release-verification playbooks plus loader tests.
  - Robustness: squad/round2-robustness — malformed Codex log handling and JobQueue validation/unknown-job semantics.
- Lead integration combines these changes on squad/full-squad-round2, bumps package metadata to 0.13.0, adds a coverage dependency/report, updates release notes, and records verification limits.
- Tests were authored for each change, but full-suite execution is not yet verified. GitHub Actions runs repeatedly report runner_id=0 and empty step lists. Do not mark the lane changes green or merge until a real runner reports passing tests.

## 2026-10-28 — Self-evolution guard round (v0.13.1 candidate)

- Source review found weak string-prefix path containment, unvalidated proposal IDs in metadata operations, an agent-controlled shell test-command surface, and per-command rather than whole-proposal timeout accounting.
- Candidate fixes validate canonical worktree containment, proposal IDs, edit sizes/counts/duplicates, direct pytest commands, shell quoting, one deadline across setup/test/retry/commit, and duration telemetry.
- Added tests for sibling-prefix escape, symlink/absolute path escapes, metadata path traversal, edit bounds, shell-command rejection, duration caps, reset safety, and evolution status reporting.
- Also corrected FastAPI's version string to track the package version.
- Execution note: local GitHub access from this sandbox fails DNS resolution, and connected Actions jobs have no assigned runner or executed steps. Candidate must not be labeled green until tests run on a functioning runner.


## 2026-10-28 — Secure dispatch and controlled hosting (v0.14.0 candidate)

- Created `squad/secure-node-dispatch-gateway` stacked on `squad/evolve-loop-guards`.
- Added central dispatch validators for strict run/message payloads, route uniqueness and hop ceilings, supported modes, numeric bounds, prompt/message caps, node URL safety, HTTPS-by-default peer links, and explicit opt-in for legacy non-loopback HTTP.
- Hardened fleet registration, persisted peer loading, outbound request sizing, signed-header shape validation, and bounded replay nonce storage.
- Added API-token checks for remote dashboard, chat, tools/extensions, gateway, telemetry/map, run logs, OSINT/evolution status, and the event WebSocket. Reverse-proxy deployments should use AVRONE_API_AUTH=always.
- Scrubbed application/provider/fleet/cloud credentials from agent child-process environments; local host-root runner execution now fails closed by default.
- Added a restricted operator-only root operation broker for allowlisted system service status/restart. It requires the existing admin token, a separate strong gateway token, an application allowlist, and independent least-privilege host policy. No arbitrary root shell was exposed to agents.
- Added cross-process UTC-day self-evolution reservation accounting: default 900 seconds/proposal, 3600 seconds/day, 12 attempts/day; hard ceilings 1800 seconds/proposal, 14400 seconds/day and 100 proposals/day. Worst-case durations are reserved before work starts to prevent parallel proposals oversubscribing the cap.
- Added outbound HTTP policy for agent fetch and generic HTTP requests: reject local/private/link-local/metadata targets, resolve and reject non-public DNS answers, validate every redirect, strip credentials across origins, reject redirects for write methods, bound DNS validation time and cap decoded response bodies at 4 MB. DNS rebinding still requires egress firewall/host network controls.
- HTTP request bodies are capped at 1 MB and headers are count/byte/control-character bounded. Redirect chains share a total deadline, and all non-loopback targets are revalidated. Process timeouts now kill POSIX process groups and Docker commands run under an in-container timeout as a second guard.
- Reject non-finite JSON constants in signed fleet requests, persisted fleet state and daily budget files so NaN/infinity cannot invalidate time-budget comparisons. Daily budget counters/duration fields are schema-validated and symlinked budget files are refused.
- Made fleet registry persistence atomic and symlink-safe; validate persisted load/capabilities/children/presence fields, reject future timestamps, and bound registry and stored presence data.
- Added targeted test modules for dispatch validation, runner secret isolation/root guard, server auth modes, restricted host service operations, and daily budget contention/corrupt-state handling.
- Verification remains pending. GitHub source reads/writes work through the connector, but shell cloning is unavailable in this environment; prior Actions jobs failed without assigned runners. This branch must not be called green until its exact commit runs the full pytest matrix and bundle job successfully.

- Added exact private HTTP destination opt-in via `AVRONE_HTTP_PRIVATE_ALLOWLIST` so operator-authorized internal fleet nodes can be reached without opening all RFC1918/ULA space. The entry is an exact hostname or IP only; wildcards/CIDRs are ignored, and loopback/link-local/reserved/metadata endpoints remain denied.
