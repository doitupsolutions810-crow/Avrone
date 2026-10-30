---
name: fleet-restart-safety
description: Restart, redeploy or move a fleet node without reopening replay of signed requests, and confirm the node rejoined cleanly.
---
1. Before the restart, replay_guard should read OK and persisted=yes once the node has taken any
   signed request. The replay cache lives in the data dir, so the data dir must survive the restart
   (a volume, not the container's own layer); a fresh data dir is a fresh node.
2. After the restart, run replay_guard again. "boot guard: active" means the saved cache could not
   be read: for two windows the node refuses anything signed before it booted, and peers simply
   re-sign. It clears by itself; state_health shows whether fleet_nonces.json was set aside.
3. DEGRADED with "memory-only" means the node has no data dir for the cache - fix the deployment,
   do not work around it. DEGRADED with "near its cap" means a peer is flooding signed requests;
   check fleet_status for the noisy node before raising anything.
4. Confirm rejoin with fleet_status: heartbeats resumed, health not hot, and no privileged grant
   (AVRONE_FLEET_PRIVILEGED) appeared that was not there before.
5. research_note the restart and the replay_guard result.
