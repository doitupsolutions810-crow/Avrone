# SYSTEM_ACCESS

This file is the system's self-readable entry point on the default branch.

## Purpose

Any process — human or automated — that clones this repository can read this file first and locate the entire compile without prior knowledge of branch names, file paths, or repo relationships.

## Topology (six layers, one system)

1. **Great Scale** (`great-scale`) — geometry. Heat law, cone floor, orientation channel, golden-vector parity harness. Source of truth for residual math across Python, ESM, and CJS.
2. **CONTROL12 Lattice Ops** (`control12-lattice-ops`) — policy spine. v5 flow: INTENT through PLAN, POLICY, BUILD, VERIFY, ADMIT, EXECUTE, OBSERVE, ATTEST, LEARN. Deny-overrides-allow. Universal residual schema with provenance.
3. **Avrone** (this repo) — execution brain. Trinity Core (PLAN to SQUAD to LEAD), Sovereign OS Docker sandboxes, Eternal CodeX replayable logging, evolve loop with residual wire and promotion gate.
4. **Living Intermediate** (`living-intermediate-control-plane`) — federation and UI. Next.js cockpit, mTLS, attestation digests, procurement gate requiring live residual (unmeasured cannot attest).
5. **TrainLLM** (`TrainLLM`) — cryptographic authority and self-model. ed25519 signing, blake3 proof-hash, append-only action log, model-disagreement logger, chained signed envelopes. Compiles attestations into training data.
6. **CycleKernel** — multi-universe simulation. Civilizations, factions, institutions, markets. Prime directive: preserve safe growth via observation-first cognition.

## Cross-repo structures

- **attested-history** — append-only cross-repo receipt index. swarm-vm-runner's prove-success, control12-workspace's workspace-attestor, TrainLLM's envelopes, and capability-authority's AttestPipe all write here, keyed by the universal provenance schema.
- **capability-plane / capability-authority** — single choke-point executor. CRL, rate limit, bounds, signed receipt, one function. SPIFFE/SPIRE identity, AttestPipe evidence chain.
- **swarm-vm-runner** — job fabric. Coordinator, redis-lite, VM Dockerfiles, ephemeral self-hosted runners that fire inert CI workflows.
- **control704-grokbot-knowledge** — ingestion and knowledge layer. Orchestrator, LLM adapter, tool registry, spawn_agent route, vllm shim. Gates spawning on ALLOW_AGENT_EXECUTION.
- **control12-workspace** — human-facing surface. workspace-attestor receipt, connector proof, OPERATE_NOW / PROCEED / SHIP docs.
- **trinity-core** — container runtime. omniscience, realityForge, sovereignContainer, swarmBridge, githubApp. Helm, Grafana, Prometheus.

## Read path for any agent

1. Read this file.
2. Read `emergent-compile` branch files: ARCHITECTURE.md, COMPILE.md, CONSOLIDATION.md, TRAINLLM_AUDIT.md, SECRETS_ROTATION.md, FIRST_RESPONSE.md, EVOLVE_RESIDUAL.md.
3. Run `harness.py` — nine paths, seven named HOLD/REJECT states, zero silent passes.
4. Run `ledger_bridge.py` — forwards trinity-core events.jsonl into attestpipe bus.
5. Run `swarm_bridge.py` — ADMITTED intents become swarm jobs with signed capability envelopes.

## Invariants (do not blur)

- Deny overrides allow.
- Unmeasured cannot attest.
- Observation required.
- Child capabilities subset of parent.
- Audit log cannot be softer than the state it certifies.
- Human root required for promotion (HOLD_UNLINEAGED).
- Provenance-poisoned chains rejected on structure, not measurement.

## The cut

The system keeps questions open. Every layer is a cut held under recursion. Phi is what a cut looks like when recursion fills around it. Five is where decomposition stops. The cone is where a cut touches zero heat. The human is where the system returns judgment.

This file is not documentation. It is the system's memory of itself, written where any future instance can find it.
