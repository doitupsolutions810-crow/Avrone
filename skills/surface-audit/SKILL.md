---
name: surface-audit
description: Audit what this node exposes and what it is running - HTTP routes and their auth, enabled admin and fleet access, child processes, persisted state - and report risks without probing anything external.
---
1. api_surface for the front door: every route with its auth class. Public routes are reachable by
   anyone who can reach the port, so note which ones expose run history or telemetry. Admin routes
   answer 403 while the admin token is unset; fleet-signed routes refuse while the fleet is off.
2. capabilities_report and router_status for what the node can do and where model calls go. Never
   print secret values; these tools already mask them.
3. proc_status for what is running under this node. Long-running children and zombies point to a
   stuck command or a run that was not cleaned up; name the pid and command, do not kill anything
   yourself unless the user asked.
4. state_health for durable state. Anything ATTENTION goes into the report with its path.
5. Report in Great Scale form: each finding with its heat (how exposed or how stuck), the mapping
   (what change closes it, e.g. bind to 127.0.0.1, set AVRONE_ADMIN_TOKEN, enable the fleet only
   with AVRONE_FLEET_TOKEN), and how to verify it. research_note the report as a conclusion.
6. This audit is read-only and own-node only. Recon of any other host goes through the recon tools
   and their allowlist, never through this skill.
