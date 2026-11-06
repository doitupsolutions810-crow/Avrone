# Extended edge-case round — November 3, 2026

This round tests the Python/Node bridge and receipt protocol. Model responses in the contract tests are explicit test doubles; successful tests do not establish live model inference or production readiness.

## Results observed

| Scope | Result |
| --- | --- |
| Final unfiltered Python suite, including the complete Mac launcher matrix | **3,690 passed, 7 skipped, 0 failed** on Python 3.12.14, four workers, in 844.72 seconds |
| Measured Python statement coverage | **99.53%**: 12,248 of 12,306 statements; 58 uncovered |
| Initial adversarial Python run before fixes | 22 failed, 15 passed; failures reproduced real protocol and evidence-handling defects |
| Bridge Python checks, including wire and soak cases | 96 distinct cases passed across the focused and broader runs |
| Broader core selection, excluding the Mac autopilot file | 3,321 passed, 4 explicit capability skips |
| Node contracts | 49 passed, including 14 new edge-test blocks |
| Deterministic corpus | 1,500 valid JSON inputs, 1,500 byte mutations, 1,500 worker-shape variations, 1,500 snapshot variations, and 1,000 invalid Node prompt variations |
| Sustained real HTTP contract test | 240 requests across 20 bursts; concurrency at most four; overload returned 429; accepted receipts were unique and matched persisted CodeX events |
| Mac launcher matrix | All 357 cases included in the final unfiltered run: 354 passed, 3 skipped. The initial run exposed four failures; corrected cases passed or explicitly reported unavailable process visibility. |
| Remote CI at publication commit `75739fec6642b0be1321217c51223858c14e4ea2` | Runs `41583349849` and `41583348183` failed/cancelled with runner ID 0 and zero executed steps; no remote test pass is claimed |

The seven skips were checked explicitly: four require matching runtime/procfs PID namespaces, two cannot exercise unreadable files or read-only directories while running as root, and one requires the optional `zxingcpp` barcode dependency. No failing test was reclassified as a pass.

## Defects corrected

- The Python decoder rejects duplicate JSON keys, exponent overflow, invalid UTF-8, lone surrogates, nonstandard constants, excessive nesting and oversized bodies. The Node decoder rejects non-finite numeric values and invalid Unicode scalars. Bridge URLs reject controls, backslashes and query/fragment delimiters before credentials are sent.
- Apply a total bridge deadline including streamed body reads. Refuse encoded bodies, cancel oversized Node streams, and keep upstream exception details out of error responses.
- Preserve the complete bounded research answer so its returned content still matches the receipt digest. Lead input uses explicitly labelled excerpts; retained receipt evidence remains complete within its bounds.
- Require the current local run ID and the requested number of usable worker reports. Explicit failure, HOLD, running status or mismatched mode metadata overrides otherwise polished text. Whitespace, unusual worker names, failed workers and exhausted turns cannot be promoted by a polished Lead answer.
- Snapshot finite JSON evidence within a component budget. Malformed or oversized components produce bounded HOLD evidence instead of serialization errors or unreadable multi-megabyte receipts.
- Bind stored receipt identity to the requested run ID. Use directory-relative file descriptors, no-follow opens, regular-file checks, exclusive temporary files, atomic publication and syncs. Reject symlink directories, nonregular entries and existing receipts; failed writes clean only files created by that write.
- Release council slots on cancellation and storage failure. Cancelling a council stops its pending components; cancelling its Lead cannot publish a COMPLETE receipt.
- Enforce research admission limits, strict credential types and matching Unicode code-point limits. A failed start/completion audit write withholds a successful research receipt; CodeX exposes a persistence acknowledgement.

The Mac launcher fixes retain the tracked idle sleep across ignored HUP/USR1 signals and collect descendants before terminating their parents. The test harness now preserves the selected virtualenv interpreter and disables automatic Homebrew-path recovery only when testing missing Git. The whole-tree termination test remains explicitly unavailable in a Linux environment whose procfs describes a different PID namespace; that condition does not skip the test on macOS.

## Reproduction

Use an activated Python environment with `.[dev]` installed, plus Node 22+:

```sh
python -m pytest -q tests/test_bridge_adversarial.py tests/test_grokbot_integration.py tests/test_bridge_wire.py
python -m pytest -q --cov=avrone
npm --prefix integrations/grok-bot test
python scripts/verify-source-registry.py
```

The recorded unfiltered run used `pytest-xdist` with `-n 4`; it is an optional test-runner dependency, not a runtime dependency. The environment places its virtualenv on PATH because evolution checks start Python subprocesses. Loopback and TestClient traffic bypass the environment's HTTP proxy.

The wire fixture starts a real Node HTTP server and writes real temporary CodeX files. Its agent function is a deterministic test double. Docker builds, persistent browser lifecycle and actual model-driven bridge execution remain live gates. A physical Mac has not been exercised by these Linux launcher simulations.
