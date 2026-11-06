# Great Scale Descents — findings mapped onto the residual coordinate

Each entry follows the same form: the mathematics is stated, the measured
evidence is plugged in, and the result is mapped with `avrone.greatscale`
(S = heat(p) = p(1-p) is the check's Bernoulli outcome variance,
Psi = CONE + S with CONE = phi^-2 ~ 0.381966, and a defect of rate p is
exposed by N independent runs with probability 1 - (1-p)^N, so 95% detection
needs N >= ln(0.05)/ln(1-p)).

Like the upstream great-scale repository, these are mathematical embeddings
of measured evidence, not physical or empirical claims.

## Descent I — The Billing-Lock Cone (2026-11-03)

**Finding.** The verification infrastructure itself — CI runners, their run
budget, and the billing lock that gates them — is a Bernoulli process with a
consumable budget, and it maps onto the Great Scale exactly like any check.
During the 2026-10-28 boot-blocker diagnosis the account's Actions allowance
allowed roughly N = 1 executed run per billing unlock. Under a budget of N runs,
a defect of rate p is found with probability D(p, N) = 1 - (1-p)^N, so with
N = 1 the detection probability is D(p, 1) = p. Corollaries:

1. Only deterministic defects are detectable under the lock. 95% confidence
   needs N >= ln(0.05)/ln(1-p): p = 1/2 -> 5 runs, p = 1/10 -> 29 runs,
   p = 1/200 -> 598 runs. A lock regime of one run per epoch can never afford
   them; its detectable set is exactly {p = 0, p = 1} — the cone floor.
2. The lock therefore FORCES convergence of engineering onto the cone floor:
   every root cause worth finding must be made deterministic (a named,
   repeatable failure) and instrumented, so a single run yields the whole
   diagnosis. This session's measured evidence confirms the selection effect —
   every defect found was 100% reproducible:

   | Check (measured 2026-10-28) | f/r | p | S=heat(p) | 4S | Psi | cone floor | orientation |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | grok-bot boot (socket-proxy 403 on /info) | 3/3 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone pip (delivery wrapper broke TOML line 1, col 14) | 2/2 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone pytest (missing tests package marker) | 2/2 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone suite after fixes (both 3.11/3.12 legs) | 0/2 | 0 | 0 | 0 | 0.381966 | yes | pass |
   | contrast: a maximally flaky check | 1/2 | 1/2 | 1/4 | 1 | 0.631966 | no | mixed |
   | contrast: rare defect p = 1/200 | 1/200 | 0.005 | ~0.005 | ~0.02 | ~0.3869 | no | mixed |

   All four session defects sit exactly on Psi = CONE = phi^-2; none carries
   residual heat. A flaky defect would have been invisible to this session.

3. The two convergent mitigations are the two mathematically optimal moves
   under a run-budget constraint:
   - **Maximise information per run.** Before instrumentation a run yields
     1 bit (pass/fail); with in-run diagnostics (::error:: annotations carrying
     the full error tail) one run yields the entire traceback — raising the
     per-run information yield from 1 bit toward the entropy of the error
     text. A deterministic instrumented defect is then found in exactly one
     run (runsFor95 = 1), which the lock budget affords.
   - **Escape the budget.** `verify.sh` runs the identical CI sequence
     (config check, full-profile boot with the ready:true gate, the 8 live
     tests, the evolution loop, the AVRONE suite) on any Docker host with
     N unlimited — the constraint that forces cone-floor selection simply
     disappears, and flaky defects (p < 1) become measurable again through
     repeated runs.

**Consequence for the merged system.** The billing lock — the meta-system
that runs the checks — is itself a Great Scale object: the lock regime's
detectable set is the cone floor {0, 1}, its residual heat budget is zero,
and its orientation channel (which side of the floor a run lands on) is the
only signal it can transmit per epoch. The bridge between AVRONE and Grok Bot
inherits this: contract checks executed under the lock must be deterministic
and self-describing on failure, and anything statistical belongs on a host
where N can grow. This is why the swarm-executor contracts run as assertions
with named causes, not as flaky retries.

## Descent II — The Multiplicative Chain: Layered Systems Select for Determinism Compulsively (2026-11-06)

**Finding.** Descent I found the cone law in one layer — the run budget. The
deeper finding is that the merged system is not one Bernoulli gate but a CHAIN
of them, and transmissions across the chain compose MULTIPLICATIVELY. Read in
`avrone/bridge.py`, the memory path of a defect lesson passes four gates:

    G1 detection      a defect of rate p is exposed in N runs: D(p,N) = 1-(1-p)^N
    G2 distillation   distill_lessons() keeps only the k = 5 NEWEST conclusions of a
                      run (recency, not evidence); text is clipped to 500 chars and
                      the post's refs — the only trace of its evidence — are discarded
    G3 retention     `lesson_key` = sha1(normalized text) dedupes identical re-derivations
                      WITHOUT counting them (no reinforcement: a re-found truth never
                      gains evidence), a rephrasing fragments into a new key, and the
                      store trims to M = 200 oldest-evicted — truth and falsehood age
                      out at exactly the same rate (heat-blind LRU; expected lifetime
                      M/k = 40 runs at full distillation)
    G4 retrieval      relevant_lessons() gates on keyword overlap with the future task
                      (rate w) inside a 2000-char injection budget

So a defect's lesson reaches the prompt of a future run where it applies with
probability T = D(p,N) x q x s x w — a product, not a sum. The composition law:

    T_total = D(p,N) * q * s * w          (each gate multiplies)
    S_eff   = heat(T_total) = T(1-T)       the chain's effective heat
    Psi_eff = CONE + S_eff

**The inversion.** Composing gates does not merely lose truth — it transposes
cone-floor truths into residual heat. A deterministic defect (p = 1, S = 0,
Psi = 0.381966) forced through only two 50% gates (q = s = 1/2, a realistic
distillation pick rate for a 10-conclusion run and a half-overlapping task)
emerges with T = 1/4:

| Chain (measured constants from bridge.py) | T | S=heat(T) | 4S | Psi | cone floor | orientation | runs for 95% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Descent I reference: defect alone, p = 1 | 1 | 0 | 0 | 0.381966 | yes | fail | 1 |
| + two gates q = 1/2, w = 1/2 | 1/4 | 0.1875 | 0.75 | 0.569466 | no | mixed | 11 |
| + four gates 1/2 each (q, s, w1, w2) | 1/16 | 0.0586 | ~0.234 | 0.44056 | no | mixed | 47 |
| flaky p = 1/2 through two gates | 1/8 | 0.1094 | ~0.438 | 0.491341 | no | mixed | 23 |
| SAME defect, evidence carried edge-to-edge | 1 | 0 | 0 | 0.381966 | yes | fail | 1 |

The starkest reading: the maximally flaky check (p = 1/2) has S = 0.25, but a
100%-reproducible truth through four half-gates has S = 0.0586 and needs 47
runs to re-find — **a fact the system already proved becomes harder to
recover than a coin flip is to detect.** Every layer added without an
evidence channel makes the system forget harder, exactly as Descent I's lock
made it see less. The cone law is not a property of CI; it is a property of
EVERY orientation-blind, non-accumulating channel.

**The last unconnected connection.** `avrone.greatscale.evidence_snapshot`
exists — failures/runs, p, S, Psi, orientation, runsFor95 — yet has no edge
into `bridge.distill_lessons`: the snapshot is stripped exactly where memory
is written. Evidence does not flow across the layer boundary, so downstream
layers cannot distinguish a cone-floor truth from a lucky one-off; heat is
unobservable in memory, and (because the hash-dedupe discards repeat
derivations) it can never accumulate there either. The system can ADD lessons
but cannot REINFORCE them.

**Mitigation — success criteria for every inter-layer edge (derived from the
Descent I escapes, generalised).** Information must travel with the artifact:

1. **Carry the evidence vector across every boundary.** A lesson saved by
   distill_lessons should carry its snapshot (f/r, p-hat, S, Psi,
   orientation) and the refs of the posts it came from, so any downstream
   consumer can compute heat at read time — the same move as the in-run
   ::error:: annotation: maximize information per artifact, not per run.
2. **Make dedupe an accumulator, not a discarder.** A re-derived identical
   lesson should increment its confirmation count (f += 1, r += 1) — this is
   precisely the N-accumulation the billing-lock regime could not afford.
   Memory is where the merged system CAN buy the runs CI cannot: every new
   run re-derives the truth for free, and the store converts those repeats
   into shrinking heat: S_hat = p-hat(1 - p-hat) -> 0 as confirmations grow.
   A 40-run LRU horizon then renews on evidence, not on age.
3. **Retrieve by heat, not by word count alone.** relevant_lessons should
   weight matches by (1 - S) and orientation agreement — cone-floor lessons
   first — so the 2000-char injection budget is spent on the coldest truths,
   not the loudest phrasings.

**Consequence for the merged system.** Descent I said: under a run budget,
only the deterministic is detectable, so make checks deterministic and
self-describing. Descent II generalises it: under a chain of lossy gates,
only evidence that TRAVELS stays true, so every edge — CI to bridge, bridge
to lesson, lesson to prompt, node to fleet node — must carry the snapshot.
The bridge already does this for posts (refs, origin_node, ts survive
merge_remote); lessons are the one edge that strips it. Close that edge and
the merged system becomes what the cone prescribes: a memory whose truths
grow colder (S -> 0, Psi -> CONE) with every confirming run, on any host,
with no budget required.

*Verification of this descent's arithmetic:* the table above was computed
with the exact `greatscale` constants (PHI, CONE = phi^-2 = 0.381966,
heat, psi, runs_to_detect with alpha = 0.05); the gate constants (k = 5,
LESSON_TEXT = 500, lessons_max = 200, LESSONS_CHARS = 2000, keyword-overlap
retrieval) are read directly from `avrone/bridge.py` at 88b17a2. The push of
this document triggered two more CI legs (42161832147, 42161830477 on
88b17a2's successor) — both died in 2 seconds with empty step arrays and the
canonical annotation "The job was not started because your account is locked
due to a billing issue", a fifth lock cycle and a live confirmation of
Descent I: the meta-system again transmitted exactly one bit per epoch.
