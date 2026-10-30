---
name: agent-pack-hygiene
description: Keep agent packs (portable agent homes + memory) trustworthy - verify before import, clear refused or stale packs, and keep secrets out of them.
---
1. pack_list verifies every pack in memory and prints OK or the exact refusal: unexpected member,
   link, traversal, sha256 mismatch, over quota, or "expands past the quota" (a decompression bomb).
   Nothing is extracted, so it is safe to run on a pack you did not make.
2. Import only packs that read OK, and only your own: agent_pack_import refuses another agent's pack.
3. A REFUSED pack from a peer is a finding, not a retry: note the reason and which peer sent it
   (fleet_status, peer_audit). Delete it rather than editing it.
4. Before agent_pack_export, run secret_scan on the agent home: a pack copies the home and its
   memory to whichever node imports it.
5. Packs count toward disk like logs; the quota is AVRONE_AGENT_FS_QUOTA_MB. Keep the newest good
   pack per agent and research_note what you removed.
