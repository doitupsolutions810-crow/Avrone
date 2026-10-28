# AVRONE daily engineering memory

## 2026-10-28 — v0.12 edge-coverage follow-on

- Resumed from `doitupsolutions810-crow/Avrone`. The live `main` head advanced during review to `2f7cca8` (v0.12.0), containing the recon allowlist fix, port validation, MCP squad-size clamp/version report, and offline CLI/MCP/LLM/recon suites.
- Added OSINT aggregation hardening so malformed JSONL, JSON scalars/lists, non-string event kinds, malformed payloads, and non-string host values are skipped rather than crashing metrics.
- Added offline tests for OSINT aggregation edge records, MCP invalid parameter shapes/early task validation, and fleet online/degraded/offline freshness plus auth errors.
- Added a CI source-archive job: after the Python 3.11/3.12 matrix passes, upload `avrone.zip` as artifact `avrone-<commit-sha>` for 14 days.
- Verification caveat: shell cloning cannot resolve `github.com` in this execution environment. Existing CI runs show `runner_id=0` with no executed steps, so no green GitHub Actions result is available yet. The follow-on branch must not be merged until a real green test result is observed.
