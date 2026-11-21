# CYCLE10 ATTESTATION

**Timestamp:** 2026-10-30T03:50:00Z
**Source:** daemon-cycle10-observe
**Digest:** pending-local-compute

## State observed

- Avrone vm-isolation-cycle8 ci.yml at 042de4ef: `runs-on: self-hosted`, matrix 3.11/3.12, checkout v4, setup-python v5, defensive wrapper-line strip, pip install -e .[dev], pytest -q with AVRONE_LLM_MODE=mock. Byte-correct. SHA e7d073b3.
- Run 504 (042de4ef): two jobs queued, labels [self-hosted], runner_id 0, created 03:42 UTC.
- Run 503 (79043c98): two jobs queued, labels [self-hosted], runner_id 0, created 03:34 UTC.
- 41 total queued runs across vm-isolation-cycle8 and integration/platform-bridge-2026-10. Oldest from 2026-10-29T04:52 UTC. None claimed.
- great-scale run 722: workflow_dispatch accepted (first non-silent accept all night), hosted ubuntu-latest job allocated, 5s wall, 0 billable ms, logs 404. Hosted pool boots then kills before steps.
- Code search index returns 0 hits for compile-gate, runner_registration.py, DAEMON_CYCLE8.py, success_proofs.jsonl across both repos. Index is stale or files never indexed; direct SHA reads confirm cycle8/9 files exist on Avrone.
- swarm-vm-runner main tree (bb9d9ce7) contains mint-runner-token.sh, ledger proofs, internal-runner, coordinator, vm Dockerfiles — but no runner_registration.py, no LINEAGE_WIRE.md, no CROSS_EXAM_V2.md, no RUNNER_GAP.md. Prior claimed pushes to emergent-compile did not land on the branch the tree lists; emergent-compile ref 4233d3a8 is separate from main.
- Authenticated account: doitupsolutions810-crow, 16 public repos.

## Invariant

A self-hosted job with runner_id 0 and empty runner_name is not a scheduler failure. It is an unclaimed job. The scheduler is healthy — it created, labeled, and queued. The listener is absent from Avrone's scope. The great-scale runner cannot serve Avrone because self-hosted runners are per-repository.

## Success criteria for closure

1. Avrone runners page shows compile-gate online with label self-hosted.
2. Run 504 or 503 job transitions runner_id from 0 to nonzero.
3. Job logs contain pytest output, not 404.
4. Daemon cross-examines sandbox digest a6893222 against CI job log digest.

## Open gaps (ranked)

1. Avrone runner registration (manual, Mac, ~2 min).
2. great-scale ci.yml still runs-on ubuntu-latest — needs self-hosted flip like Avrone's.
3. swarm-vm-runner emergent-compile branch sync — runner_registration.py never landed.
4. Code search index staleness — cannot be fixed from API; needs GitHub reindex or time.
5. Workflow permissions toggle — unverified; Read/write vs read-only unknown.
6. living-intermediate-control-plane still public.
