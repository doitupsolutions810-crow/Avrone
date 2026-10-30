---
name: agent-home-resilience
description: Check your own agent home (path, quota use, container hosting, home node), keep it under the agent-fs quota, and make it survive thermal migration between nodes with verified pack export and import.
---
1. agent_home first. It reports YOUR OWN home only: its path relative to the workspace (agents/<your id>),
   bytes used against the AVRONE_AGENT_FS_QUOTA_MB quota (default 50 MB, hard cap 1024), the per-file cap
   (AVRONE_AGENT_FS_MAX_FILE_MB, default 5 MB), the file count, whether the home is container-hosted, your
   home node and your last pack/migration record. Use {"format": "json"} when you want to compute on it.
   If it says you have no agent home, per-agent fs is off (AVRONE_AGENT_FS=0): keep state in memory_save
   instead and stop here.
2. Read the container line. With AVRONE_AGENT_CONTAINER=persistent and the docker runner, your commands run
   in one long-lived container that mounts only your home at /agent; it is removed after
   AVRONE_AGENT_IDLE_SECS idle (default 600). Only /agent survives that reap, so keep anything you need
   later inside your home, never elsewhere in the container's filesystem. If docker=no or mode=off, your
   home is a plain directory on this node.
3. Keep under quota. When used_pct is high or OVER QUOTA shows, `fs_list` with recursive true to find the
   big files, `fs_read` to check one before you drop it, and `fs_delete` what you no longer need (a
   subdir needs recursive true). Writes through `fs_write` are refused once a write would pass the quota,
   so clean up before a big write, not after it fails.
4. Remove symlinks and special files. agent_home warns about them: they are never followed, and
   `agent_pack_export` refuses a home that holds any. `fs_delete` removes a symlink itself, never what it
   points at.
5. agent_pack_export before risky or long work, and before a node may get hot. It bundles your home dir and
   your own memory namespace into a deterministic, hashed pack under agentpacks/ and prints its name
   (<your id>-<created>.tar.gz) and sha256. It refuses a home over the quota, so step 3 comes first.
   Run agent_home again: the newest pack line confirms it.
6. agent_pack_import with {"name": "<your id>-<created>.tar.gz"} restores one of YOUR packs on this node. It
   verifies every hash and path, only replaces an existing home with a strictly newer copy whose home
   node matches yours; memory merges newest-wins. A refusal leaves your home untouched.
7. Thermal migration. When the node runs over AVRONE_THERMAL_MAX_C (default 85) and AVRONE_AGENT_MIGRATE=on
   with a fleet (AVRONE_FLEET_TOKEN), idle agent homes are moved to a cooler peer automatically. To move
   your own home on purpose, `agent_migrate` with {"target": "auto"} (the coolest / least-loaded node) or a
   node id; the receiving node must also run AVRONE_AGENT_MIGRATE=on. If it fails, your home stays here.
8. After a migration or an import, agent_home again. The home node stays where you were born (it travels
   in the pack), the record shows migrated_to / imported, and born_here is false on the node that took you
   in. Check the file count and bytes match what you expected before carrying on.
9. memory_peek to confirm your memories came across with the home (your own namespace only).
10. Report: your home path, usage against quota, whether it is container-hosted, your home node, and the
    pack name and sha256 of your newest export.

11. Thermal and migration uncertainty: if a temperature reading is missing, stale, or contradictory,
    pause heavy parallel work, preserve a checkpoint and report the value as unknown. Never disable CPU,
    memory, thermal, or container limits to keep reconnaissance running.
12. Before resuming on another node, verify the pack hash, home quota, memory namespace, and last migration
    record. If any verification fails, stop and report the mismatch rather than continuing with partial state.
