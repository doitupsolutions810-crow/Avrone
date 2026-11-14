# Local Ollama + Docker agent sandbox

This is the supported local operator profile for AVRONE. It runs the AVRONE control plane on the host, connects it to local Ollama, and sends each agent's shell commands to a persistent Docker container with only that agent's private home mounted.

## Security boundary

- AVRONE_RUNNER=docker, AVRONE_AGENT_FS=1, and AVRONE_AGENT_CONTAINER=persistent.
- One lazily created avrone-agent-<node>-<agent-id> container per worker identity.
- A single bind mount per agent: its own workspace/agents/<id> at /agent; no shared workspace mount.
- A non-root container UID:GID matching the host owner of the 0700 agent directory. A root AVRONE host process is refused.
- Read-only container root filesystem, a 128 MB /tmp tmpfs, HOME=/agent, no capabilities, no-new-privileges, 512 MB RAM and equal swap cap by default, 1 CPU, 512 PIDs, and a bounded open-file limit.
- --network none for agent containers and the shared execution environment unless the operator explicitly changes the profile. Ollama stays on the host, so the AVRONE server can reach it at loopback while the agents cannot reach the network.

The Docker daemon remains a privileged host service. Do not run AVRONE itself inside a container with /var/run/docker.sock mounted merely to enable this profile. Run the AVRONE Python server as your normal unprivileged host user; it invokes Docker CLI to create the constrained sibling containers.

## Operator setup

Install and start Docker Desktop for Mac (or Docker Engine on Linux) and install Ollama first. On macOS, Ollama can be installed from [ollama.com/download](https://ollama.com/download); Docker Desktop is available from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop). Do not run the AVRONE server as root.

**Terminal 1 — Ollama service.** Open the Ollama app, or start the service manually and leave this terminal open:

~~~bash
ollama serve
~~~

**Terminal 2 — AVRONE.** From the repository root:

~~~bash
cp docs/local-ollama/local-ollama-sandbox.env.example docs/local-ollama/local-ollama-sandbox.env
ollama pull qwen2.5:3b
docker info
python3 -m pip install -e ".[dev]"
bash docs/local-ollama/start-local-ollama-sandbox.sh
~~~

The launcher refuses to proceed unless Docker is running, Ollama answers both native and OpenAI-compatible API probes, the selected model is already installed, the AVRONE package imports, and the host process is a non-root user whose UID:GID matches the private agent directory owner. It does not open external network access for agent containers.

**Terminal 3 — live integration test.** With AVRONE still running:

~~~bash
python3 docs/local-ollama/test-local-ollama-squad.py
~~~

The smoke test posts a real two-worker mode=squad request to the loopback /api/chat endpoint, confirms that Ollama and Docker are the active backends, requires both workers to call the real shell tool, reads the actual Eternal CodeX run events, and inspects each corresponding agent container. It checks non-root identity, read-only root filesystem, only-/agent mount, disabled network, dropped capabilities, no-new-privileges, and resource limits. A natural-language answer without tool events and matching container evidence is a FAIL.

The report is written under data/local-ollama-smoke/<run_id>.json. The test does not pull a model or image, change host configuration, or modify repository files.

## Useful checks

~~~bash
docker ps --filter label=avrone.agent-home=1
docker inspect $(docker ps -q --filter label=avrone.agent-home=1)
curl -fsS http://127.0.0.1:8710/api/status
~~~

The per-agent home is lazily created on first shell use and reused across commands. Its container is reaped after AVRONE_AGENT_IDLE_SECS (600 seconds by default). The private host directory survives container removal. For cleanup, stop the AVRONE server and inspect the labelled containers before removing them; do not delete all Docker containers indiscriminately.

## Local model selection and benchmarking (November 11, 2026)

Keep qwen2.5:3b as the known AVRONE baseline until alternatives pass the real harness. The library now lists Qwen3.8 27B (18 GB, 256K) as a newer, higher-memory Qwen-family comparison; this replaces the earlier Qwen3.6 suggestions in this guide. The repository README reports an earlier no-mock score of 100/100 for this model on a 2-vCPU host; that is a historical repo claim, not a current result from this run.

Recommended candidate order for a fresh benchmark (checked against the live Ollama library on November 11, 2026):

| Model | Current Ollama listing | Why test it |
|---|---:|---|
| mimo-v2.6:9b | 6.6–8.7 GB, 256K context | New 9B code/agent workflow distillation, tool use and thinking; strongest first upgrade candidate |
| granite4.2:8b | 5.3 GB, 128K context | Tool use, structured JSON, coding and RAG; Apache 2.0 |
| qwen3.8:27b | 18 GB, 256K context | Newest Qwen agentic-coding candidate in the current Ollama library; vision, tools and thinking; workstation-class option |
| nemotron-3.5-lightning:30b | 19–25 GB, 1M context | 30B MoE / 3B active, designed for always-on agents; larger model footprint despite low active count |
| laguna-xs-2.1:q4_K_M | 20 GB, 256K context | Linux/CUDA candidate for now; avoid macOS/Metal until the currently reported empty Chat output issue is resolved |

Do not pull the large candidates blindly on a memory-constrained Mac. In particular, avoid Laguna XS 2.1 on macOS/Metal until its reported empty Chat response issue is resolved; its current Ollama model card explicitly calls this out. Check free disk and unified memory first, pull one candidate at a time, and retain a reproducible record of model tag, Ollama version, quantization, context length, host hardware, latency, tool-call success and squad score. Ollama model listings and tags change; verify before pulling.

Run the existing no-mock AVRONE benchmark (it really executes the shell tool) against installed models:

~~~bash
python3 tools/model_bench.py --base http://127.0.0.1:11434 --models qwen2.5:3b,mimo-v2.6:9b,granite4.2:8b --no-deep --out data/model-bench-local.json
~~~

The benchmark may pull missing tags depending on its implementation; pre-pull only after reviewing model storage requirements. Keep the first comparison small. Upgrade the worker model only after the Actions+tool and 2-worker squad lanes pass; test a different AVRONE_LEAD_MODEL separately before enabling a mixed-model profile.


### How to read the first benchmark

A model's advertised parameter count, active parameter count, or context window is not a substitute for an AVRONE tool-use result. The current local bench ranks protocol correctness, action JSON validity, actual shell execution, a two-worker squad and Lead synthesis. Add future tests for checkpoint resume, duplicate tool calls, malformed tool arguments, model timeout, partial worker failure and a second Lead model. Keep the run ID, exact model tag, Ollama version, host memory and hardware, quantization, latency percentiles and raw JSON together. Do not promote a new default from a one-off answer-quality comparison.

The local AVRONE server also exposes `GET /api/model/capabilities` when configured for Ollama. It reads `/api/show`, `/api/tags`, `/api/ps`, and `/api/version` without pulling or loading a model; missing feature metadata is returned as `null`, not inferred. Use it to inspect tools/vision/thinking support, model context metadata, configured `num_ctx`, quantization, installed digest and the running context/VRAM footprint before selecting a worker or Lead model.

## Integration architecture: keep the working parts, add capability adapters

1. Trinity remains the control plane. It already owns PLAN/SQUAD/LEAD, concurrency, checkpoints, worker failure isolation and CodeX events. Do not introduce a second scheduler just to add a new SDK.
2. Ollama stays behind the OpenAI-compatible Chat Completions adapter. Its compatibility layer supports chat completions, tools, JSON/structured outputs, streaming and reasoning controls, but it is a subset of the OpenAI API; for example tool_choice and logprobs are not supported in the current compatibility matrix. AVRONE should continue to own its tool loop and validate its own JSON action envelope rather than relying on unsupported provider-specific switches.
3. Keep execution separate from inference. Ollama requests originate from the host-side AVRONE process. Agent containers have no network. If a future architecture moves Ollama into Docker, place it on a dedicated inference-only network and never attach worker containers directly to that network.
4. MCP needs a dual-version migration, not a protocol-version string bump. This repository's client and server currently advertise MCP 2025-06-18, implement the initialize handshake and retain Mcp-Session-Id. The official 2026-07-28 revision removes protocol sessions and the initialize exchange for Streamable HTTP, makes requests self-describing via _meta, adds server/discover, requires Mcp-Method / Mcp-Name routing headers, and introduces Multi Round-Trip Requests. Keep 2025 clients working while adding a separately tested 2026 stateless transport path; use explicit AVRONE run/checkpoint IDs as application handles. Add conformance tests before advertising 2026 support.
5. OpenAI Agents SDK is a reference adapter, not a replacement orchestrator. It now supports container-based sandbox agents, handoffs/agents-as-tools, guardrails and built-in tracing. The sensible integration is an optional adapter that exposes selected AVRONE specialists as typed tools to an external Agents SDK workflow, and/or wraps selected external Agents SDK specialists behind Trinity tools. Preserve CodeX as AVRONE's durable evidence log and correlate external trace IDs with its run IDs. Do not migrate scheduling, persistence or permission checks out of Trinity without a separate design and test gate.
6. Make model and MCP capabilities explicit. Add a provider capability record (tool calls, JSON schema, vision, streaming, reasoning, context window, max parallelism) populated from Ollama /api/show and model metadata. Use it to select safe defaults and to skip unsupported request fields. Keep model tool-call results and container execution results separate in CodeX so a provider hallucination cannot be mistaken for tool execution.

## Current verification boundary

Repository-side unit tests can validate Docker argv and policy without a daemon. The live Chat/Ollama/container test requires this Mac's Docker daemon, installed Ollama model and running AVRONE server. If Desktop Commander is not connected, those checks are BLOCKED until the host is connected; do not treat offline tests as evidence of live execution.
