---
name: agent-node-runtime
description: Safely inspect Node.js/npm, provision a tiny loopback-only Node health server inside a persistent per-agent container, and keep its files and memory within that agent's home.
---
1. runtime_preflight first. Confirm Python, runner and workspace readiness before attempting application work.
2. node_runtime with format=json checks Node.js and npm versions in the active runner. It runs only fixed version commands; it does not install packages or start a process.
3. agent_home checks your own workspace quota, symlink warnings and persistent-container status. Do not infer that a container port is published to the host.
4. node_service with action=scaffold writes only the known `node-server.js` health-server template in your own agent home via quota-enforced `fs_write`. It refuses symlink targets and never overwrites a different existing file.
5. node_service with action=start requires `AVRONE_RUNNER=docker`, `AVRONE_AGENT_CONTAINER=persistent`, and Node.js already installed in that agent container. If Node is missing, stop and report it; do not install packages at runtime. An operator can build `infra/docker/agent-node/Dockerfile` and set `AVRONE_DOCKER_IMAGE=avrone-agent-node:22` before starting AVRONE.
6. The health server listens on `127.0.0.1:8765` inside this agent's container only. No Docker host port is published. Use node_service action=status to check `/health`, and action=stop to ask the matching service to exit.
7. fs_list to inspect only your own workspace and identify existing project files before writing. Keep all generated project files within the reported quota and per-file cap.
8. shell may run a reviewed, bounded command in your own execution environment. Do not turn arbitrary shell input into an application API, expose an unauthenticated listener on `0.0.0.0`, or bypass the agent-home boundary.
9. memory_save to persist a concise project manifest, build status and last known-good revision; do not store credentials or private key material in memory.
10. agent_pack_export before long-running work or planned thermal migration. Verify the exported pack and memory namespace before relying on recovery.
11. evolve_start for a long self-evolution loop, then evolve_status and evolve_history to follow its progress and recovery. Resume only interrupted loops under the same or lower caps; never disable command timeouts or resource limits.
12. Report Node.js/npm availability, the service status, the per-agent-home scope and any limitations. Never claim that the loopback URL is externally reachable.
