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
