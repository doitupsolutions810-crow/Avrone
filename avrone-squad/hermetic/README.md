# Hermetic Harness — Proposal I

The evolve loop clones application code. The invariant suite lives here, in a
separate tree the loop can read but never write.

## Why
A self-evolving system that verifies itself with tools it can rewrite has no
external witness. Every prior mitigation in this descent — adaptive gates,
scaled failure costs, circuit breakers — assumed the test suite was trustworthy.
It isn't, if the suite is inside the clone. This proposal draws the boundary
the loop cannot cross.

## How it works
1. `harness_client.py` reads `PINNED_SHA`, fetches the harness repo at that
   exact commit, content-hashes the tree, and compares.
2. Mismatch or fetch failure aborts the evolve before any application test runs.
3. Invariants execute as gate stage-one. Stage-two (unit tests) only runs if
   stage-one is green.
4. Human-only writes: edits to the harness require a direct push. No evolve
   path exists.

## The Gödel layer
This is not a permission system. It is the resolution of the incompleteness
the descent kept circling: a system rich enough to describe its own verification
cannot prove its own consistency from inside. The harness repo is the
metalanguage. AVRONE is the object language. The human's quarterly read of the
invariant tests is the one external act the theorem requires.

## Success criteria
- Evolve payloads containing harness paths: zero reach the clone (parser rejects)
- SHA mismatch: gate aborts before tests (fail-closed)
- Invariant suite: eight tests, all documented, all version-pinned
- Human duty: one commit per quarter, PINNED_SHA confirmed, CHANGELOG appended
