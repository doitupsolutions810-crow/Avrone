---
name: edge-node-health
description: Check this node's governor and thermal health, pick healthy fleet nodes for heavy work, and fall back to running locally when the fleet is unavailable or unhealthy.
---
1. capabilities_report first: it says whether you have shell and network access under current settings,
   so you do not plan work this node is not allowed to do.
2. shell `avrone-cli doctor` from the repo root to read the node's configuration checks, including the
   fleet_offload and squad_dispatch toggles. Note any WARN lines about an open AVRONE_FLEET_ALLOW.
3. python to read this node's own sensors with the governor (avrone/governor.py: Governor(max_parallel=4).read() for the
   readings, is_hot() for the thermal verdict): load per CPU, mem_pct, disk_free_pct and temp_c. The node is HOT when temp_c is over AVRONE_THERMAL_MAX_C (default 85). The governor
   starts cutting parallelism 15C below that, above 1.0 load per CPU, above 80% memory and below 10% free disk.
   AVRONE_GOVERNOR=off only stops throttling; the sensors are still read.
4. think: decide where the work runs.
   - Healthy node and a small task: run it here, nothing more to do.
   - Hot, throttled, or the squad is bigger than the governor allows: prefer the fleet.
5. If a fleet is configured (AVRONE_FLEET_TOKEN set), list nodes with the fleet_nodes tool. Only "online"
   nodes carry a trusted health block. Prefer cool, idle nodes; skip hot or throttled ones. Passing node "auto"
   to remote_spawn or queue_submit picks the healthiest allowlisted child for you, so use "auto" unless the task
   needs a specific node.
6. Never send privileged work to another node to dodge a hot local node: privileged runs are never offloaded.
   Respect AVRONE_FLEET_ALLOW and AVRONE_FLEET_MAX_HOPS; a refusal is final, do not retry around it.
7. Fall back locally when: AVRONE_FLEET_TOKEN is unset, no node is online, every node is hot or throttled, or a
   remote run fails or times out (AVRONE_FLEET_OFFLOAD_TIMEOUT, default 300 s). Run the task here at reduced
   size (a smaller squad n) instead of waiting on the fleet.
8. For long runs on a node that may overheat, add a checkpoint_id (see the checkpoint-resume skill) so a thermal
   shutdown costs only the unfinished steps.
9. memory_save key "fleet.last_health" with the date, local readings and the node you picked; then report where
   the work ran and why (healthy local, offloaded to a node, or local fallback with the reason).
