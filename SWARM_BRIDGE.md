# Swarm Bridge

Closes the last runtime gap: the harness proves gates against a shadow executor.
This module makes the executor real without touching production.

## Topology

```
Harnee ADMIT -> swarm_bridge.admit_to_swarm() -> queue/*.json
                                              -> swarm-vm-runner consumes
                                              -> ledger entry with parent pointer
                                              -> attested-history index
```

## The one link that matters

`envelope.parent_decision` points back to the harness's ADMITTED record.
`verify_job_against_index` is the success criterion: a job whose parent
can't be found in the index is unmeasured, and unmeasured cannot attest.

## What it reuses

- TrainLLM's CapabilityEnvelope schema (ed25519 spine signs the digest)
- attested-history's provenance format
- CONTROL12's deny-overrides-allow (expired scope, bad signature reject)
- Great Scale's residual_s field (the job carries its own heat)

## What it does not do

- It does not execute anything. It only queues.
- It does not bypass any gate. An intent that failed ADMIT never reaches here.
- It does not soften the poison check. A poisoned chain's job is still poison.
