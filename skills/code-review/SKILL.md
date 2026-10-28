---
name: code-review
description: Review code changes for correctness, regressions, security boundaries, tests, and evidence-backed release claims.
---

# Code review and regression analysis

1. Restate the requested behavior and identify the exact files, entry points, and callers that can change it.
2. Trace existing input validation, side effects, error handling, and compatibility before proposing a refactor.
3. Review the diff in small units. Look for malformed input, unexpected types, empty values, path traversal, concurrency races, partial writes, retries, and cleanup after failure.
4. Add focused regression tests for the bug and neighboring boundary cases. Keep unit tests offline by mocking HTTP transports and sockets; mark real-network or multi-server tests as integration.
5. Run the narrow test file first, then the full suite. Record the exact command, exit code, and relevant failure output. Never describe a proposed or unexecuted test as passing.
6. Cross-check that test names match asserted behavior and that tests fail against the pre-fix implementation when practical.
7. Check logging for secrets, tokens, private data, unbounded output, or attacker-controlled newlines.
8. Summarize: changed behavior, risks, evidence, remaining coverage gaps, and exact commit/branch. Do not merge changes solely because a patch looks plausible.

## AVRONE-specific invariants

- Keep AVRONE_RECON and AVRONE_RECON_ALLOWLIST gates intact; infrastructure recon is one explicit authorized host per call.
- Never add tooling to locate, deanonymize, or profile private people.
- Self-evolution proposals stay in throwaway clones; accepted changes must pass the configured test gate and do not implicitly modify main.
- Be truthful about model/provider, sandbox, network, runner, and test capabilities.
