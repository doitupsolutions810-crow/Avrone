---
name: agent-node-runtime
description: Check Node.js and npm in the active per-agent execution environment and keep local services within their declared isolation boundary.
---
1. runtime_preflight first. Confirm Python, runner and workspace readiness before attempting application work.
2. node_runtime with format=json checks Node.js and npm versions in the active runner. It runs only fixed version commands; it does not install packages or start a process.
3. agent_home checks your own workspace quota, symlink warnings and persistent-container status. Do not infer that a container port is published to the host.
4. fs_list to inspect only your own workspace and identify existing project files before writing.
5. fs_write to create or update a Node project file only inside your own agent home. Stay within the reported quota and per-file cap.
6. shell may run a reviewed, bounded command in your own execution environment. Do not use arbitrary shell input as an application API, and keep service listeners on loopback unless an operator has approved a specific network/port policy.
7. memory_save to persist a concise project manifest, build status and last known-good revision; do not store credentials or private key material in memory.
8. agent_pack_export before long-running work or planned thermal migration. Verify the exported pack and memory namespace before relying on recovery.
9. evolve_start for a long self-evolution loop, then evolve_status and evolve_history to follow its progress and recovery state. Resume only an interrupted loop using its checkpoint and the same caps.
10. Report Node.js/npm availability, whether the command ran in the per-agent home, workspace quota state, and any limitations. Never claim that an internal service is externally reachable unless its network path and port publication have been verified.
