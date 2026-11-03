# Signed Swarm objective dispatch

Use `dispatch: "swarm"` to queue the whole objective through the Capability Authority and Swarm receiver. Queue acceptance leaves task completion unconfirmed; no local planner or worker runs on this path. The default dispatch mode remains local. The explicit Swarm Compose override selects Swarm unless `AVRONE_SQUAD_DISPATCH` overrides it.

Configure the pinned Authority/Swarm identities, Ed25519 trust keys and private key files in `compose.swarm.override.yml`. Provision the requester/issuer keys with `scripts/provision_swarm_keys.py`; obtain the signed parent capability from the Authority operator. The key and parent files require mode 0600 inside a mode-0700 directory. `AVRONE_SWARM_PARENT_CAPABILITY_FILE` selects the parent file; `AVRONE_CA_PARENT_CAPABILITY_FILE` is a compatible alias. Conflicting paths fail closed.

The operator API accepts:

```json
{
  "message": "the exact approved objective",
  "mode": "squad",
  "dispatch": "swarm",
  "swarm_correlation_id": "operator-job-001"
}
```

POST this body to `/api/chat` with the current `X-Admin-Token`. Swarm mode always requires that token, including when public chat is enabled or the mode comes from the environment. A previous blackboard token does not authorize dispatch. CLI operators can run:

```sh
avrone-cli squad 'the exact approved objective' --dispatch swarm \
  --swarm-correlation-id operator-job-001 --json result.json
```

Use the same correlation ID and exact objective after a timeout. Objectives contain 1–2000 characters; IDs contain 1–128 letters, digits, dots, underscores, colons or hyphens. Without an explicit ID, a new run uses its run ID. The durable SQLite ledger remembers a checkpoint’s submission identity independently of local worker count and checkpoint-file integrity. A retry may omit a previously supplied correlation ID; a conflicting replacement is refused. Deleting the local checkpoint does not mint a new Swarm identity. Use a new checkpoint ID for different work. The SQLite database defaults to `<data_dir>/swarm-outbox.sqlite3`; `AVRONE_SWARM_OUTBOX_DB` overrides the path for both API and Trinity. Keep that database on durable storage and share it when multiple processes serve the same workload.

Before contacting the Authority, AVRONE binds the message/correlation IDs to the objective and persists the exact signed requester assertion. A restart reuses that request without rereading the parent file. Once an enqueue is persisted, retries reuse its exact signed envelope without redelegation. Independent SQLite connections share dispatch ownership; an expired owner cannot record a queue result. Reusing either identity for different work is refused. A changed Swarm audience cannot retarget an existing delegation.

Results report `queued`, `pending`, `rejected` or `disabled`. Queued/pending runs emit corresponding events and never `run.done`. A queued result is not a specialist execution receipt. Missing trust, Authority rejection, ambiguous transport and storage failures do not trigger local fallback. Privileged contexts cannot export objectives. Deep mode requires an existing first answer when the squad default is Swarm, so it cannot mistake queue acceptance for a completed first wave.

The explicit `/api/swarm/enqueue` endpoint remains available for a supplied child capability or automatic delegation with stable `message_id` and `correlation_id`. Its retry and request-status routes operate on persisted signed enqueues. See [FUSION_SWARM_VALIDATION.md](FUSION_SWARM_VALIDATION.md) for tested software behavior and the remaining deployed Authority/worker gate.
