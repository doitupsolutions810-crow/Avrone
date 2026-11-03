# Integration validation — updated November 3, 2026

## Source pins

- AVRONE baseline: `9036ff6adc67655211357ab5bed38051c11cc999` (`main`, merged A12LR daemon).
- Grok Bot import: `65d526f24e667d10121640cb2baeb2c3c26b54ef` (`codex/inspect-integration-hardening`).
- `avrone/static/source-registry.json` records imported byte hashes and local implementation changes. Source pins alone do not certify the modified implementation; its local hashes are recorded separately.

## Observed results

| Check | Observed result |
| --- | --- |
| Final unfiltered Python suite, including the Mac launcher matrix | **3,690 passed, 7 skipped, 0 failed** on Python 3.12.14, four workers, in 844.72 seconds |
| Measured Python statement coverage | **99.53%**: 12,248 of 12,306 statements |
| Grok Bot deterministic Node contracts | **49 passed** |
| Expanded adversarial corpus | 7,000 deterministic input variations across Python and Node |
| Sustained real HTTP contract test | 240 requests across 20 bursts; admission cap and recovery, unique receipts and matching persisted audit events passed |
| Imported JavaScript syntax and AVRONE page scripts | Passed |
| Combined Compose structure, service references, loopback bindings and CI YAML | Passed structural checks; actual Docker Compose validation unavailable here |
| Real Uvicorn process and HTTP APIs | Startup, status, tool list and council page passed |
| Council with unavailable model/peer | Returned `HOLD`; receipt persisted and retrieved with matching digest |
| Docker/Compose preflight | `BLOCKED`: Docker CLI and daemon are absent |
| Live inference | Unavailable: no reachable Ollama service or configured model credentials |
| Browser on the operator's Mac | AVRONE loopback connection refused; the application could not be opened there |
| GitHub publication | Authorized by the owner and published as [draft PR #17](https://github.com/doitupsolutions810-crow/Avrone/pull/17); publication source tree verified against the local tree |

The full final suite supersedes earlier partial runs. The extended malformed-input, evidence, receipt storage, cancellation, Unicode, overload and wire checks, together with the defects they exposed and corrected, are documented in [EDGE_CASE_VALIDATION.md](EDGE_CASE_VALIDATION.md).

The seven skips comprise four process-visibility checks with mismatched runtime/procfs PID namespaces, two permission checks unavailable under root, and one optional `zxingcpp` barcode dependency check.

The HTTP smoke server was started and stopped for testing. It is not a persistent deployment on the operator's Mac. The wire contracts use explicit model test doubles. They verify protocol and persistence behavior, not actual model reasoning. The launcher matrix simulates Mac dependencies on Linux; it does not establish behavior on a physical Mac.

## Remaining live gates

Run `python3 scripts/grok-stack.py up --open` on a Docker host, then `python3 scripts/grok-stack.py verify`. A successful release still requires real image builds, the persistent browser CDP connection, boot self-tests, live forward and reverse bridge calls, and a `COMPLETE` council receipt. Model quality, independent-report truth and container-based self-evolution are not established by contract tests.

GitHub Actions runs `41583349849` and `41583348183` for the prior publication commit `75739fec6642b0be1321217c51223858c14e4ea2` failed/cancelled with runner ID 0 and zero executed steps. No remote CI pass is claimed; the PR remains draft. The local unfiltered suite completed successfully as recorded above.
