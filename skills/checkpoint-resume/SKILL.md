---
name: checkpoint-resume
description: Run long squad or deep runs with a checkpoint_id so an interrupted run (power, thermal shutdown, dropped session) resumes from its finished steps instead of starting over.
---
1. Use checkpoints only for squad and deep runs: chat mode refuses a checkpoint_id. Pick an id of 1-64
   characters from A-Z a-z 0-9 _ - that names the job, e.g. "audit-2026-10-29". Write it down: memory_save key
   "checkpoint.<id>" with the exact task text, mode and size, because a resume must repeat all three.
2. Start the run with the id:
   - CLI: `avrone-cli squad "<task>" --checkpoint-id <id>` or `avrone-cli deep "<question>" --checkpoint-id <id>`
     (or --checkpoint PATH for a file you manage yourself).
   - HTTP: POST /api/chat with "checkpoint_id": "<id>" and mode squad or deep.
   - On another node: remote_spawn with mode squad or deep and the same checkpoint_id; the receiving node keys
     it per sender, so retrying the same job with the same id resumes it there.
3. Progress is recorded as the run goes: the plan, each finished worker or reviewer result, each completed deep
   round, then the whole result. Failed worker results are never stored, so a resume retries them.
4. To resume, re-run the SAME task, mode and size with the SAME id. Finished steps are skipped. If the task
   differs, the server refuses with a conflict (HTTP 409): either re-post the original task, choose a new id, or
   delete the old checkpoint.
5. Never reuse one id for two different jobs, and never pick an id another sender is using; a checkpoint that
   belongs to another owner is refused.
6. Admins inspect checkpoints with GET /api/checkpoints and GET /api/checkpoints/<id>, and remove a finished or
   abandoned one with DELETE /api/checkpoints/<id> (needs the admin token). A checkpoint in use by a live run
   cannot be deleted.
7. A corrupt or unreadable checkpoint file is treated as empty and the run starts fresh; a failed checkpoint
   write never breaks the run. So a checkpoint saves time, it is not a guarantee: still report partial results.
8. Report the checkpoint id, whether this was a fresh start or a resume, and which steps were reused.
