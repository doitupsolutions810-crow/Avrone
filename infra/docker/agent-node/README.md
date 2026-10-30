# Python + Node.js per-agent container image

The optional image adds Node.js/npm to the Python agent runtime. It is intended for AVRONE's **persistent per-agent Docker containers**, not for granting host-level privileges.

## Build

From the AVRONE repository root, build the image with:

```bash
docker build -f infra/docker/agent-node/Dockerfile -t avrone-agent-node:22 .
```

## Configure AVRONE

Set these environment variables **before** starting AVRONE:

```bash
AVRONE_RUNNER=docker
AVRONE_AGENT_CONTAINER=persistent
AVRONE_AGENT_FS=1
AVRONE_DOCKER_IMAGE=avrone-agent-node:22
```

Then restart AVRONE so newly created agent containers use the image. Existing persistent containers are not automatically rebuilt just because this setting changes; review and recreate them under your normal maintenance process.

## Run the internal Node health server

The agent can call `node_runtime` to verify Node/npm, then `node_service` with `action=scaffold` and `action=start`. The starter service is deliberately minimal: `GET /health`, a controlled `GET /__stop`, and 404 for other paths. It listens on `127.0.0.1:8765` inside that one agent container. Docker does **not** publish the port to the host. The host dashboard and other agents cannot reach it through the container network by default.

Use `node_service` with `action=status` and `action=stop` to check or stop the service. Its source is written through the normal `fs_write` tool so the per-agent quota rules apply; the tool refuses a symlink target or an existing `node-server.js` with different contents.

This image does not enable privileged mode, add Linux capabilities, publish ports, or elevate the agent to host root. Do not remove the fleet gateway's capability checks to make a local service reachable.
