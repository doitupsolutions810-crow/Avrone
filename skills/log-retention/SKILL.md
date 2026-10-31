---
name: log-retention
description: Keep this node's run logs from filling the disk - measure, clean secrets first, then prune old logs while keeping recent and active runs.
---
1. log_health first if anything about the history looks off: never prune a log you have not read.
2. run_logs reports how many run logs exist, their total size, the oldest and newest, and how many
   would be pruned with the current settings. It changes nothing without prune.
3. Before pruning, secret_scan the run logs. Pruning deletes evidence: if a secret is flagged,
   decide on rotation first, then fix or prune.
4. run_logs with prune set to true deletes logs older than older_than_days (default 30) but always
   keeps the newest keep logs (default 50, never fewer than 10) and every run still active on this
   node, including the current one. Pick older_than_days from how far back the user audits runs.
5. state_health afterwards confirms the data dir is still consistent, and codex_stats confirms the
   recent history is intact.
6. research_note the before and after sizes so the next run can tell whether a schedule is needed.
