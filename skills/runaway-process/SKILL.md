---
name: runaway-process
description: Find and handle a stuck or runaway command started by this node - a shell or python step that never returned, a zombie, or a loop that kept a child alive - without losing the run's work.
---
1. proc_status shows children running longer than AVRONE_PROC_LONG_S and any zombies. Use
   {"all": true} to see every child with its age, state, memory and command.
2. Match each long-running child to its run: codex_search with the command text or run_id shows
   which agent started it and when.
3. If it belongs to an evolve loop, evolve_status shows the loop; evolve_cancel stops it cleanly and
   its runner kills the child when the run is cancelled.
4. If it is a plain shell step, the agent's next command can stop it (kill <pid>) only when the
   user asked for it or it is clearly this run's own stuck step; say which pid and why.
5. Zombies are reaped when the parent waits; one that persists means its parent is stuck, so
   report the parent instead.
6. Run proc_status again to confirm, then research_note the cause so the next run avoids it.
