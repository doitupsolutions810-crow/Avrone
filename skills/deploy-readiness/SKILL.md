---
name: deploy-readiness
description: Decide whether this node is ready to run on a VPS, Fly.io or Railway - model route, exposure, state health, resources and runaway processes - and produce a go/no-go list.
---
1. router_status: which model provider each call takes and whether its circuit is closed. A local
   Ollama primary needs the host to keep the model resident; local-model-ops covers sizing.
2. exposure_check: must read LOCAL or GUARDED. AT RISK is a no-go; follow exposure-hardening.
3. state_health: must be HEALTHY. Anything set aside is fixed first with state-recovery.
4. capabilities_report and env_report for runner, network and resource limits. A local runner
   with shell on a public host is a no-go unless AVRONE_PUBLIC_ACCESS is admin or loopback.
5. proc_status: no long-running children or zombies before the move.
6. Write the go/no-go as a short list with the setting or command for each item, in Great Scale
   form (heat, mapping, verification), and research_note it. Choosing the platform and paying for
   it is the operator's call; present the trade-offs, do not sign up for anything.
