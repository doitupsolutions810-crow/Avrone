---
name: release-verification
description: Prove a hardening change before calling it shipped - reproduce the finding on the old version, show the new one refuses it, and leave the node's own state clean.
---
1. Write the finding as a probe that runs against a real server (python -m avrone.server on its own
   port and data dir), not only a unit test: TestClient hides some failures (it cancels the app,
   it never streams a body the way a socket does).
2. Run the same probe on the previous release and on the change. A finding counts only when the old
   version shows it and the new one does not; keep both numbers (status codes, memory, timings).
3. Gate the change: the full suite, the integration marker, and coverage must not drop.
   flake_probe re-runs suspect read-only tools; registry_audit confirms every tool still loads with a
   schema.
4. Check the node after the probe: state_health for corrupt state files, proc_status for leftover
   children, limits_status for caps left at probe values, run_logs to prune probe runs.
5. research_note the finding, both probe results and the version that fixed it.
