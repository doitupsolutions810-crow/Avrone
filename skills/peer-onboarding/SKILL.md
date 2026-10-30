---
name: peer-onboarding
description: Add a node to the fleet or review existing peers so this node only ever calls addresses it should.
---
1. Decide who may join first: AVRONE_FLEET_ALLOW lists node ids, and AVRONE_FLEET_REQUIRE_ALLOW=1
   refuses everyone when that list is empty.
2. The joining node registers with its own url. Since v0.31 a url must be plain http(s): no
   credentials, query or fragment, and never a link-local or cloud metadata address. LAN and
   loopback urls are fine.
3. peer_audit after any join and after upgrading an old node: it checks every saved peer against
   these rules and shows status. A BAD URL peer is never called; ask its operator to re-register.
4. replay_guard should read OK on both sides, and fleet_status should show the new peer online with
   heartbeats.
5. Privileged work stays off unless AVRONE_FLEET_PRIVILEGED=allow names the node in
   AVRONE_PRIVILEGED_NODES. Joining never grants it. research_note the join.
