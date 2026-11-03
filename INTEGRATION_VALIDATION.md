# Integration validation — November 2, 2026 (Detroit)

## Source pins

- AVRONE baseline: `9036ff6adc67655211357ab5bed38051c11cc999` (`main`, merged A12LR daemon).
- Grok Bot import: `65d526f24e667d10121640cb2baeb2c3c26b54ef` (`codex/inspect-integration-hardening`).
- `avrone/static/source-registry.json` records imported byte hashes and local implementation changes. Source pins alone do not certify the modified implementation; its local hashes are recorded separately.

## Observed results

| Check | Observed result |
| --- | --- |
| New Python integration regression tests | 20 passed on Python 3.12 |
| Grok Bot deterministic Node contracts | 35 passed |
| Imported JavaScript syntax and both AVRONE page scripts | Passed |
| Grok console inline JavaScript | Passed |
| Combined Compose structure, service references, loopback bindings and CI YAML | Passed structural checks; actual Docker Compose validation unavailable here |
| Real Uvicorn process and HTTP APIs | Startup, status, tool list and council page passed |
| Council with unavailable model/peer | Returned `HOLD`; receipt persisted and retrieved with matching digest |
| Docker/Compose preflight | `BLOCKED`: Docker CLI and daemon are absent |
| Live inference | Unavailable: no reachable Ollama service or configured model credentials |
| Browser on the operator's Mac | AVRONE loopback connection refused; the application could not be opened there |
| Core suite (Mac autopilot sweep excluded) | 3,252 passed, 4 skipped; one fleet-server setup timeout passed on isolated rerun (1 passed in 3.57s). |
| Mac launcher cases affected by root-container harness | 5 passed; the complete long autopilot sweep was interrupted and is not certified. |
| Corrupt/tampered receipt retrieval | 3 cases rejected with HTTP 409 |
| GitHub publication | Automatic approval review rejected the push; no remote branch or PR was created. |

The HTTP smoke server was started and stopped for testing. It is not a persistent deployment on the operator's Mac. Its report is evidence of runtime behavior with missing services, not evidence of working model agents.

The initial exact-baseline attempt stopped after 208 passes, 2 skips and 5 failures caused by the Mac autopilot's root-refusal guard in this root-owned execution environment. The test harness now opts in only for its synthetic root-container tests and explicitly removes that opt-in when checking root refusal. Production root protection is unchanged. The broader run found two handoff regressions, which were corrected by regenerating MASTER_PROMPT.md and recognizing dotted tool names. The offline port-cap test now injects its DNS answer instead of requiring external DNS. Three process tests require matching runtime/procfs PID namespaces and explicitly skip when that capability is unavailable. Runtime process status now reports unverified visibility instead of a misleading zero count. The test environment must include its virtualenv on PATH because the evolution gate starts Python subprocesses. The full unfiltered suite and its total coverage remain uncertified.

The focused handoff, offline DNS, process-visibility and integration checks also completed with **35 passed, 3 explicit capability skips**. The three corrupt-receipt API cases were checked after the broader run. No production or all-green unfiltered-suite claim is made.

## Remaining live gates

Run `python3 scripts/grok-stack.py up --open` on a Docker host, then `python3 scripts/grok-stack.py verify`. A successful release still requires the real image builds, persistent browser CDP connection, boot self-tests, live forward and reverse bridge calls, and a `COMPLETE` council receipt. Model quality, independent-report truth and container-based self-evolution are not established by contract tests.

The current `main` Actions run observed during this session was `41574742062`, conclusion `failure`. A new feature branch must be checked independently; this integration does not inherit a green CI status.
