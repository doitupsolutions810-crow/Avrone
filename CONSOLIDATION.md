# CONSOLIDATION — One System, Five Ledgers, One Choke Point

## The finding

The compile is not twenty-six repos. It is one system built five times, each lineage forgetting the others existed. Every repo contains the same four primitives — gate, receipt, ledger, forge — each copy slightly softer, slightly more trusting than the last.

- capability-plane's executor: sole side-effect path (CRL, rate limit, bounds, signed receipt)
- control704's orchestrator: gates on ALLOW_AGENT_EXECUTION — identical flag to control12-workspace
- control12-lattice-ops agent-capabilities.json: child must subset parent, deny overrides allow
- capability-plane attenuate: forbids amplification
- TrainLLM /token: was a public faucet for scale-sla (locked in prior commit)
- EVOLUTION_HORIZONS non-goals: long-lived god tokens, UI minting — the exact anti-patterns the compile now refuses

## The topology

```
Avrone (intent emission)
  -> CONTROL12 v5 (policy admission: INTENT->PLAN->POLICY->BUILD->VERIFY->ADMIT->EXECUTE->OBSERVE->ATTEST->LEARN)
  -> Great Scale (residual measurement: heat, psi, cone, orientation, provenance)
  -> capability-plane executor (single choke point: CRL + rate limit + bounds + signed receipt)
  -> swarm-vm-runner coordinator (job fabric: ephemeral runners, chained SHA-256 ledger)
  -> TrainLLM ed25519 spine (only signature that counts: blake3 proof-hash, chained envelopes)
  -> control12-workspace (human-facing surface: OPERATE_NOW, PROCEED, SHIP)
  -> CycleKernel (simulation: civilizations, factions, institutions — trust from evidence, not dice)
```

## Ledger consolidation

Five ledgers existed with zero cross-reads:
1. trinity-core /tmp/trinity-ledger/events.jsonl — private diary, read by omniscience only
2. capability-plane attestpipe bus
3. swarm-vm-runner ledger/ dated success proofs (Sept 17 chained SHA-256)
4. TrainLLM append-only action log (previousHash-linked)
5. control12-workspace artifacts (workspace-attestor receipt, connector proof)

Deleted four. Everything routes through the attestpipe bus via ledger-bridge.

## Success criterion (from Great Scale descent 95)

The audit log can no longer be softer than the state it certifies.
A merged compile where capability-authority's signed receipts don't bind to swarm-vm-runner's ledger proofs is just a bigger pile of unconnected numbers.
