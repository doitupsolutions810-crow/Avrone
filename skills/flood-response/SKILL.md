---
name: flood-response
description: A node is being flooded (huge requests, request bursts, peer churn) - find which cap is absorbing it, confirm the node holds, and tighten the right knob.
---
1. limits_status first: every cap with its live usage - background runs, MCP calls, body size,
   fleet peers vs the registry cap.
2. Read the refusals: 413 "request body too large" is AVRONE_MAX_BODY_BYTES doing its job (the body
   is never buffered); 429 is AVRONE_MAX_BACKGROUND_RUNS; MCP "busy" is AVRONE_MCP_MAX_INFLIGHT;
   "registry full" is AVRONE_FLEET_MAX_NODES; "replay cache full" means signed requests are
   arriving faster than they expire - check replay_guard.
3. A refusal is the node holding, not failing. proc_status and stream_status show whether anything
   is actually piling up behind the caps (child processes, backed-up dashboards).
4. Then find the source: exposure_check for routes open to the network, peer_audit and fleet_status
   for a noisy peer. Lower a cap for the flood; offboard a peer that is the flood
   (fleet-offboarding).
5. research_note what flooded, which cap held it, and any value changed.
