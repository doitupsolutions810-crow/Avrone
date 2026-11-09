# AVRONE Runtime gateway hardening verification — v0.82.1

This note records the independently tested local Runtime hotfix that the Python AVRONE integration bridge should require before any production write traffic. **It does not mean Runtime v0.82.1 is currently installed or deployed, and the GitHub Avrone application remains a separate codebase.**

## Verifiable hotfix artifact

- Archive: `avrone-runtime-v0.82.1.tar.gz`
- SHA-256: `868268433c68c9b81e05564378838dab4492c9a6a24ecaf07268a0209b7a7d84`
- Base: mounted AVRONE Runtime v0.82.0 release artifact.
- Modified runtime module: `avrone_runtime/gateway.py`.
- Additional tests: `tests/test_gateway_adversarial_extended.py`.
- Review patch bundled as `GATEWAY_HOTFIX_v0.82.1.patch`.
- The artifact is available in the originating ChatGPT conversation; it is **not** vendored or attached to this GitHub repository.

## Reproducible local evidence

Before the patch, real-HTTP adversarial tests revealed:
1. Concurrent reuse of one Idempotency-Key could generate multiple task side effects.
2. Reusing one key with the same JSON on two different HTTP operations could replay the earlier endpoint's response.

The local patch reserves keys atomically in the Runtime's SQLite database and binds the fingerprint to HTTP method, route, and canonical body. Pending keys fail closed rather than executing twice.

| Test evidence | Observed result |
|---|---|
| Full Runtime regression after patch | 342 passed, 0 failed, 14 Python multiprocessing warnings, 55.93 s |
| Selected gateway/queue/worker regression before packaging | 34 passed |
| Clean extraction + checksum and selected regression | all 3 modified-file hashes verified; 41 passed |
| Follow-on HTTP stress using clean extraction | 160 concurrent/replayed HTTP requests across 20 keys; exactly 20 tasks |
| Stress response codes | 26 HTTP 201 (including safe replays); 134 HTTP 409 while the original was pending |
| Stress error and audit status | 0 HTTP 500; Runtime idempotency reconciliation OK |

## Compatibility and rollout gate

- **Do not silently reuse idempotency keys from <=v0.82.0.** That version persisted a body-only request fingerprint. Keys with that old hash will conflict against the hardened method/path/body fingerprint. Inspect old operations and issue new keys for new requests.
- A process crash after a reservation but before finalization leaves a pending record. This is intentionally fail-closed; an operator must reconcile the durable work/receipt before acting. Do not blindly erase pending entries or claim exactly-once distributed side effects.
- Keep API gateway authentication tenant-scoped; never accept tenant identity from an agent-supplied payload in place of the API key.
- Do not merge the Runtime module into the Python Avrone app merely because the version names look related. Verify the package boundary, migration, gateway API contract, and test suite first.
- The integration bridge's `platform_bridge_create_mission` and `platform_bridge_create_runtime_swarm` tools must remain opt-in pending deployment of the hardened Runtime and real service authentication.
- Re-run the GitHub PR tests independently once a compatible self-hosted CI runner is online. There is no verified GitHub Actions green status yet for this hotfix.

Status: **local hotfix evidence complete; production rollout, GitHub merge, and deployment not verified.**
