---
name: evolve-loop-operations
description: Run, monitor, cancel and restart AVRONE's bounded self-evolve loops (evolve_loop / evolve_start) safely, using status and history to see what each loop did.
---
1. Check first that self-evolution is on: if any evolve tool answers "self-evolution is disabled on this node
   (AVRONE_SELF_EVOLVE=deny)", stop and report it. Never try to work around that switch; flipping it to deny also
   stops a running loop before its next iteration.
2. Prepare the candidates: a list of edit sets, each `[{"path": ..., "content": ...}]` replacing whole files.
   Every candidate is applied in its own throwaway clone and kept only if the full test suite passes; nothing
   touches the live tree or main, and nothing is pushed. Keep the goal one short sentence.
3. Choose foreground or background:
   - Short job you can wait on: `evolve_loop` with goal, candidates, optional max_iters, time_budget_s, test_cmd.
     It returns every attempt's verdict and the failing test tail.
   - Long job: `evolve_start` with the same arguments. It returns a loop_id at once. At most
     AVRONE_EVOLVE_MAX_BG loops run per process (default 2, hard cap 4); if refused, cancel one or wait.
4. Respect the caps: by default a loop runs at most 10 iterations and 1800 seconds. AVRONE_EVOLVE_MAX_ITERS and
   AVRONE_EVOLVE_TIME_BUDGET set the node default and can only LOWER those caps; asking for more is clamped.
   AVRONE_EVOLVE_TEST_CMD changes the gate command for every proposal. Only an operator can allow longer
   loops, by setting AVRONE_EVOLVE_LONG=allow on the node: the ceilings then rise to 50 iterations and 7200
   seconds (defaults stay 3 and 600; ask for more explicitly). Never set it yourself, and each gate run is
   still capped at 900 seconds.
5. Monitor a background loop with `evolve_status` and its loop_id (latest loop if omitted): status, iterations
   done out of max, accepted and rejected counts, budget used, and each iteration's outcome and duration.
   `evolve_list` shows every proposal and loop on the node. Poll sparingly; each iteration runs a full suite.
6. Cancel with `evolve_cancel` and the loop_id. It is cooperative: the iteration in flight finishes its clone
   and gate, the next never starts, and the loop ends with status "cancelled". A cancel request is a file, so
   it survives a restart.
7. Read finished loops with `evolve_history` (optional limit, 1..200, default 10). Done, cancelled, crashed and
   interrupted loops are kept newest-last in data/evolve/loops/history.jsonl across restarts; AVRONE_EVOLVE_HISTORY
   can only lower how many are kept (default 50, hard cap 200).
8. After a restart, a background loop whose owning process died is marked "interrupted" on disk the first
   time `evolve_status`, `evolve_list` or `evolve_history` reads it, with a resume hint, and is recorded in
   history. Loops are never resumed automatically. To continue one from its last completed iteration, call
   `evolve_start` with {"resume_loop_id": "<id>"}: it reuses the loop's checkpointed candidates (pass the full
   candidates list again if none were stored) and its earlier iterations and time count toward the same caps.
   Only an interrupted loop can be resumed; running or finished ones are refused. Otherwise re-run `evolve_start`
   with the same goal and only the candidates not yet tried, so finished work is not repeated.
9. When a loop stops with "accepted", inspect the change with `evolve_diff` and the accepted pid, and hand the
   branch to a human for review; discard proposals you will not use with `evolve_reset`.
10. Report the loop_id, how it stopped (accepted, max_iters, time_budget, exhausted, cancelled, denied,
    interrupted), iterations used, the accepted pid and branch if any, and the failing test tail otherwise.
