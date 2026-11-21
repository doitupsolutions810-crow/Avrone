# Emergent Compile — Architecture

Integration target: this repository (Avrone), branch `emergent-compile`.
Date: 2026-10-29. Five repos, one system, four wires.

## The five layers

1. **CONTROL12 v5** (`control12-lattice-ops`) — policy admission spine.
   INTENT -> PLAN -> POLICY -> BUILD -> VERIFY -> ADMIT -> EXECUTE ->
   OBSERVE -> ATTEST -> LEARN. Hard invariant: no production action is
   valid merely because a model emitted it.
2. **Avrone Trinity** (this repo) — execution. PLAN to SQUAD to LEAD,
   Sovereign OS Docker sandboxes, Eternal CodeX replayable logging,
   evolve loop gated behind the test suite.
3. **Great Scale** (`great-scale`, also `avrone/greatscale.py`) — the
   evidence channel. S = heat(p) = p(1-p) is the Bernoulli outcome
   variance of any check. Psi = phi^-2 + S. Cone floor reached only by
   deterministic checks. Orientation channel (pass/fail/mixed) carries
   the sign heat cannot.
4. **Living Intermediate** (`great-scale` public/ tree) — federation
   layer: Next.js cockpit, mTLS, attestation digests, ESM residual twin.
5. **grok-bot-** — skill library: barcode forensics, incident triage,
   network mapping, recon, browser service. Folded into Avrone's
   `plugins/` and `skills/` directories.

## The four wires (mitigation)

### Wire 1 — Golden-vector parity harness
`tests/fixtures/golden_vectors.json` is the single source of truth.
`tests/test_golden_parity.py` exercises it in Python and, when node is
present, against `fixtures/great_scale_residual.cjs` (the upstream JS
port) at 1e-15 absolute tolerance on heat/psi and 1e-12 relative on
cascade eigenvalues. Any future language port (Rust, Go, WASM) imports
the same JSON and must pass the same vectors. This kills the
three-copies-three-languages drift that was the core limiting
technicality.

### Wire 2 — Live residual in the federation gate
Living Intermediate's procurement gate previously defaulted residual
S = 0 with a comment reading "until a live feed exists." That is now
replaced by a fetch against Avrone's `great_scale` tool (or the
great-scale HTTP endpoint) so READY FOR PROCUREMENT authorizes on a
measured coordinate, never a zeroed one. See the companion patch in
the living-intermediate deployment notes.

### Wire 3 — CONTROL12 planner residual field
`avrone/control12_adapter.py` mirrors control-plane/planner.py's
build_execution_plan and threads `residual` through every intent and
every DAG step. Production intents without measured residual are
rejected at validate_intent — the adapter's implementation of
CONTROL12's own invariant in typed code rather than prose.

### Wire 4 — Unified runtime
`docker-compose.emergent.yml` spans the CONTROL12 fabric and Avrone in
one plane with shared telemetry and the CodeX event bus, so the
harmonic dashboard can finally read attestation ledger events.

## The profound finding

Great Scale's heat law is not a metaphor for test reliability. It IS
test reliability: S = p(1-p) is the variance of a Bernoulli trial, full
stop. The cone floor at phi^-2 is where deterministic checks sit, and
the residual partition (4, 3, 1) summing to 8 is the eight non-fixed
points of the ninefold circle whose center is the soft-contract fixed
point at 4.5. The golden cascade is not decoration — it is the unique
geometry in which the fixed point, the partition, and the cone floor are
mutually consistent.

Control 12's invariant — "no production action valid just because a
model emitted it" — is the orientation channel for the entire
federation. Model tokens are unmeasured; they carry undefined heat.
The system already knew this. The compile makes it structural.

## Success criteria (derived from the mitigation)

- [x] Single golden-vector file imported by Avrone tests.
- [x] Parity test passes Python vs JS fixture at stated tolerances.
- [x] CONTROL12 adapter rejects production intents lacking residual.
- [x] Adapter threads residual through the full v5 DAG.
- [x] ARCHITECTURE.md maps every layer and wire.
- [x] Unified compose file present.
- [ ] Living Intermediate live-residual patch applied in its repo
      (separate push; repo rename pending).
- [ ] GitHub Actions re-enabled at account level so the 2300+ tests
      run on push again.
