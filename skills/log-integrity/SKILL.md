---
name: log-integrity
description: A run's history looks wrong - a run view cut short, events missing, a search that misses what you know happened - check the run logs themselves before trusting or pruning them.
---
1. log_health checks the newest run logs and names each one with a finding: corrupt lines, lines
   over 4 MB, a torn last line, or more events than a run view shows. It never prints content.
2. A run view with the X-Avrone-Truncated header shows only the newest AVRONE_REPLAY_MAX_EVENTS
   events; the full history is still on disk. codex_search with that run_id reaches older events.
3. A torn last line means the node stopped mid-write (crash, kill, full disk). Every reader skips
   it; proc_status and state_health show whether the stop left anything else behind.
4. Lines over 4 MB are skipped by every reader so one huge event cannot exhaust memory. Find the
   tool that produced it with codex_search on the same run and cap that output instead.
5. Corrupt lines in many logs at once point at the disk or a second writer: check that only one
   node writes this data dir. Never edit a log by hand; secret_scan before any prune (log-retention).
6. research_note what was found and the cause.
