---
name: exposure-hardening
description: Before a node is reachable from a network, make sure nobody without a token can start agents or read runs - check the bind, AVRONE_PUBLIC_ACCESS and tokens, and say exactly which settings to change.
---
1. exposure_check gives the verdict: LOCAL (bound to loopback), GUARDED (exposed, agent and run
   routes need a token) or AT RISK. It never prints token values; neither do you.
2. api_surface lists every route with its class: public, local (loopback or X-Admin-Token),
   admin, fleet-signed. Only public routes answer a stranger.
3. For AT RISK, map each issue to one change:
   - AVRONE_PUBLIC_ACCESS=all on an exposed bind -> loopback (default) or admin
   - weak or placeholder admin/fleet token -> 32+ random characters each, never the same secret
   - remote clients need access -> set AVRONE_ADMIN_TOKEN and send it as X-Admin-Token (the UI's
     token field does this; the events socket takes ?token=)
   - Docker compose publishes on AVRONE_PUBLISH_ADDR: keep 127.0.0.1, or set
     AVRONE_PUBLIC_ACCESS=admin when publishing to the LAN
4. These are environment changes for the operator. An agent reports them; it does not rewrite the
   node's own environment or restart it.
5. Verify with exposure_check again and the doctor CLI (security.* checks), then research_note
   the result as a conclusion.
