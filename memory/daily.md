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
