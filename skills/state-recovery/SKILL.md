---
name: state-recovery
description: Find and recover from torn, corrupt or set-aside state on this node (fleet registry, evolve proposals and loops, checkpoints, agent packs, memory, run bridges) without losing good data.
---
1. state_health first. It reports, per area, how many files are unreadable, torn or set aside as
   *.corrupt-*, by relative path only; it never prints contents. HEALTHY means nothing to do.
2. Read the area before touching it. evolve_list and evolve_status show which proposals and loops
   are real; a proposal file the listing skips is partial and safe to discard with evolve_reset
   (it only ever removes that proposal's own worktree under the evolve dir).
3. A set-aside fleet.json.corrupt-<ts> means this node booted with an empty registry. Peers
   re-register on their next heartbeat; fleet_status shows them coming back. Keep the set-aside
   file for a human to inspect rather than deleting it.
4. Torn JSONL (memory, bridges) heals itself: the next append starts on a fresh line and readers
   skip the torn record. Use memory_peek or bridge_read to confirm the good records are intact.
5. An interrupted evolve loop is marked "interrupted" at startup. Resume it with evolve_start and
   {"resume_loop_id": id}; its caps and finished iterations are kept.
6. Run state_health again and research_note what broke and how it was fixed, so the next run
   recalls it.
