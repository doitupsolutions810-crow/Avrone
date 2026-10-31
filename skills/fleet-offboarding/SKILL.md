---
name: fleet-offboarding
description: Remove a node from the fleet for good - decommissioned, misbehaving or possibly compromised - so it cannot rejoin or keep being called.
---
1. Revoke first: drop the node id from AVRONE_FLEET_ALLOW (and set AVRONE_FLEET_REQUIRE_ALLOW=1 if
   the list was empty). An allowed peer that is still online re-registers on its next heartbeat.
2. fleet_forget {"node": "<id>"} removes it from the registry. While it is still allowed and online
   the tool refuses; once revoked, removal is final even if it is online.
3. peer_audit and fleet_status confirm it is gone and no other peer carries a bad url.
4. If the node may be compromised, the shared secret is too: rotate AVRONE_FLEET_TOKEN on every
   remaining node, and if it held privileged rights remove it from AVRONE_PRIVILEGED_NODES.
   replay_guard on each node should read OK afterwards.
5. Packs it sent may be suspect: pack_list verifies every pack still on disk. research_note the
   offboarding and the reason.
