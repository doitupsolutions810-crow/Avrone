# Repository upgrade review — November 3, 2026

The inventory checked 38 accessible repositories, 356 branch heads, 278 open and closed pull-request records, and 442 recent default-branch commits. Branch and PR collections were paginated to exhaustion; commit history was limited to the most recent 30 commits per repository. One repository had no commits. This is an upgrade review, not an exhaustive audit of every historical file or commit.

The public repository snapshots, PR head/base revisions and commit subjects are recorded in [REPOSITORY_UPGRADE_REVIEW.json](REPOSITORY_UPGRADE_REVIEW.json). That catalog contains the 16 public repositories. Review decisions below use revision-pinned public sources. The source registry hashes the local catalog and implementation resources.

| Reviewed source | Finding and integration decision |
| --- | --- |
| AVRONE main, `9bb0e750aa40947f7f78b6ef41dcfa9afe6ae202` | Integrate the eight new commits after the prior baseline. Keep cross-process bridge locking, compaction/replacement recovery and chaos tests. Reconcile the overlapping Grok README with the existing authenticated research route. |
| AVRONE PR #2, `fa1481329784fd47ed10731ebc007cc0d6b0686f` | Queue input validation and unknown-job behavior remain useful gaps in the current queue. Adapt them alongside ownership fencing. |
| AVRONE PR #9, `0f7c7530482b8494af3d62291e0036b0ad54e0c5` | Finite queue JSON, poison-row quarantine and valid oversized-result handling are useful queue upgrades. The whole branch also contains separate VM/identity systems; avoid importing competing runtime implementations. |
| AVRONE PRs #14 and #15 | Reviewed contract schemas, promotion gates and the research bridge file changes. Preserve the existing bounded authenticated council and its HOLD semantics. A draft or a proposed schema does not certify execution. |
| Grok Bot main, `c4d5e95b7a23639c103552ea1cc0470e86c98a21` | The socket-proxy PING/INFO boot fix is already present in the combined profile and imported Compose file. Retain it. |
| Cleanroom Agent System, `78e83cdcd546f72921124093009de850efb9466e`, `src/orchestrator.py` | Use structured per-role outcomes and persisted orchestration state as design references. Deployment and provider behavior remain AVRONE's own bounded execution paths. |
| Living Intermediate Control Plane, `5fb93800f0f23633e0dd14490af4d68ab0a4bda9`, `agents/orchestration.mjs` and `platform/python-bridge/avrone_stack/swarm.py` | Role/quorum surfaces and routed evidence are useful references. The route explicitly reports that its external model is unbound; READY planning statuses must not be treated as completed model execution. |
| Capability Authority, `012dc7d5ed254c2ece5c7c1782fb6d3eba3e08ce`, `rings/vault_lease.py` | A lease must be bound to the claimed resource and expire. Apply that principle to queue attempts using fresh claim tokens and transactional checks. The reference's lab signature is not production authentication. |
| Great Scale | Reviewed all 125 PR records, 130 branches and the recent commit window. Retain the separation between revision evidence, mathematical embeddings and observed execution. The Atlas remains a separate reviewed feature branch. |

New changes are published to the AVRONE integration branch after their targeted validation. The validation record distinguishes queue/HTTP execution, model test doubles and live model deployment.
