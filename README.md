# AVRONE

A self-hosted chat + search system where agents don't just answer, they **work**:
they get a real terminal, a workspace, network access and Docker, they spawn
sub-agents, and every action they take is logged and shown live on a HUD.

Orchestration follows the **Grok Heavy** pattern: a squad of agents attacks the
same request in parallel from different angles, then a **Lead** agent compares
their work like a study group, resolves disagreements, and writes one answer.

The model brain is your **OpenClaw** Gateway by default (any OpenAI-compatible
backend works too: xAI Grok, OpenAI, Ollama, LM Studio, vLLM).

## Architecture (from the sketch)

```
 Ghost Shell   (server.py, static/)   Web UI, REST API, WebSocket HUD, admin terminal
      │
 Trinity Core  (trinity.py)           Daemon kernel: PLAN → SQUAD (parallel) → LEAD merge,
      │                               tool loop, sub-agent spawning
      ├── Eternal CodeX (codex.py)    Event bus + permanent JSONL action log per run (replayable)
      │
 Sovereign OS  (sovereign.py)         Environment established FIRST on launch: Docker container
                                      ("VM environment") or local sandbox, health-checked
      │
 Model backend (llm.py)               OpenClaw Gateway /v1/chat/completions (or any OpenAI-compatible)
```

| Sketch item | Where it lives |
|---|---|
| AVRONE chatbot/search, website/phone/app | Web UI (mobile-friendly) + REST API any app can call |
| Chat orchestrate / Agent orchestrate | `chat` mode (one agent) / `squad` mode (Grok Heavy) |
| Agent action logs | Eternal CodeX: `/api/runs`, `/api/runs/{id}`, live Logs tab |
| Admin-level / terminal access | `/api/terminal` + Terminal tab, gated by `AVRONE_ADMIN_TOKEN` |
| Backend environment hosting so agents can install resources | Sovereign OS runner, `shell` tool |
| Long-term artifacts, Docker, network, real terminal | `background: true` runs, Docker runner, `fetch`, `shell` |
| HUD display for hosted environments | HUD tab: live agent tree, current tool, status |
| Agents spawn sub-agents | `spawn` tool (depth-limited by `AVRONE_MAX_SPAWN_DEPTH`) |

## v0.2: Deep Analysis, MCP, plugins, 17 tools

**Deep Analysis (second wave).** Give AVRONE a question plus an answer that
already exists (from you, another AI, or its own squad) and a second squad
stress-tests it: Fact-Checker, Gap-Finder, Contrarian, Deep-Diver, Forecaster,
Source-Synthesizer. The Deep Lead returns: Verdict (confirmed / corrected /
extended + confidence), Corrections, Additions, Counterpoints, Sources, Bottom line.

```bash
avrone-cli deep "Is X true?" --first-answer-file answer.txt -n 4
avrone-cli deep "Plan a home lab"            # runs wave 1 squad, then wave 2
```

**Tools.** shell, python, read_file, write_file, edit_file, list_files,
search_files, fetch, http_request, web_search (DuckDuckGo free; Brave/Tavily
if keyed), git (no force push), docker_run, memory_save, memory_recall,
artifact_save, think, spawn. Plus plugin and MCP tools.

**MCP, both ways.** AVRONE loads any MCP server from `mcp.json` (Claude Desktop
format, stdio or Streamable HTTP; see `examples/mcp.json.example`); its tools
appear as `mcp__<server>__<tool>`. And `avrone-mcp` serves AVRONE itself over
stdio with `avrone_squad`, `avrone_chat`, `avrone_deep`, `avrone_tools`, so
OpenClaw, Claude Desktop or Cursor can call the squad.

**Plugins.** Drop a `.py` with `def register(registry, settings)` in `plugins/`
(examples: calculator, current_time) or publish an `avrone.plugins` entry point.

**Providers.** `AVRONE_PROVIDER=openclaw|xai|openai|openrouter|ollama|custom`,
with `AVRONE_LEAD_MODEL` for a stronger merge model. Retries with backoff.

**CLI.** `avrone-cli squad|chat|deep|tools|runs [--json out.json] [--quiet]`.

## v0.3: Telemetry + System Map

AVRONE traces its own nodes: Trinity Core, Eternal CodeX, the Sovereign OS
runner, worker and lead models, every tool (built-in, plugin, MCP) and every
agent role, with the traffic between them built from the action logs. It never
probes outside networks.

- `GET /api/telemetry?hours=24`: runs (total, live, by mode, p50/p95 duration), agents by role, per-tool calls, errors and average latency
- `GET /api/map?format=json|mermaid`: node/edge graph, or a Mermaid flowchart you can paste into GitHub
- **Map** tab in the UI: live graph + headline metrics
- `avrone-cli telemetry` and `avrone-cli map [--format mermaid|json]`

## v0.9: Self-evolution (AVRONE edits its own code, safely)

- Agents can propose changes to AVRONE's **own** codebase with `evolve_propose` ({goal, edits:[{path, content}]}).
- Each proposal is applied in a **throwaway clone** of the repo and the **full test suite is run**. It is kept only if every test passes; a failing proposal is discarded with its test output so the agent can retry.
- Nothing is pushed and **nothing touches `main`** or the live tree. An accepted change lands on an `evolve/<id>` branch in the sandbox and is surfaced as a diff (`evolve_diff`) for a human to review and merge.
- Paths that escape the repo are rejected. Tools: `evolve_propose`, `evolve_diff`, `evolve_list`, `evolve_reset`. `GET /api/evolve` and `avrone-cli evolve` list accepted proposals.
- Config: `AVRONE_SELF_EVOLVE=allow`(default)`|deny`, `AVRONE_REPO_DIR`, `AVRONE_EVOLVE_TEST_CMD` (default `python -m pytest -q`).

## v0.8: Agent kit (tooling, skills, resources, tasks)

- **Vision tools:** `image_inspect` (size, entropy, colors, estimated barcode grid) and `image_decode` (QR, DataMatrix, Aztec, PDF417, 1D at several scales). `pip install -e ".[vision]"`.
- **Skills:** markdown playbooks in `skills/` (`skill_list`, `skill_load`). Ships with barcode-forensics, network-mapping and self-heal-environment. Add your own as `skills/<name>/SKILL.md`.
- **Self-changes, logged:** `env_install` (pip, or apt with sudo fallback). Every install, including ones run through `shell`, goes to the resource ledger (`resources` tool, `env.change` events). Policy `AVRONE_SELF_CHANGES=allow` (default) | `log` | `deny`.
- **Task board:** `task_board` lets a squad split work into subtasks with owners and status, shared across the run.
- Agents are told to load fitting skills first, track multi-part work on the board, and self-heal missing tools.

## v0.7: Gateway and model fallback

AVRONE routes model calls across several providers so agents keep working when one throttles or fails.

- Set `AVRONE_LLM_FALLBACKS=openrouter:x-ai/grok-4,ollama:llama3.1` (each entry is `provider[:model][|lead_model]`). Each provider's key comes from its usual env var.
- On a 429, 5xx or network error the gateway retries the primary, then moves to the next healthy provider; results note the route taken.
- A circuit breaker opens a provider after repeated failures (`AVRONE_BREAKER_THRESHOLD`, default 3) for a cooldown (`AVRONE_BREAKER_COOLDOWN`, default 30s), so a throttled provider is skipped instead of hammered.
- Lead and worker models are chosen per provider.
- `GET /api/gateway` shows each upstream's state, calls, errors and the last route.

## v0.6: Fleet directory, agent messaging, durable queue

- **Where every agent is:** each node reports its device (hostname, OS, arch), runner and container, network interfaces and addresses, and services (URL, tools, MCP servers, model), plus every agent with its role, status and task. Routers fold in their children, so Main AVRONE sees the whole tree. `GET /api/fleet/directory` (admin), the Map tab, and the `fleet_agents` tool.
- **Tell any agent to build:** `agent_message` tool or `POST /api/fleet/tell` with `{"agent": "router/edge/Builder-3", "message": "..."}`. A live agent gets it in its inbox and acts on its next step inside its own container; a finished one is respawned on that node. Short names work when unique.
- **Durable job queue:** SQLite (WAL) per node, atomic claims, retries with backoff, dead-letter after max attempts, crash recovery via leases. Tools `queue_submit` / `queue_status`, `GET /api/fleet/jobs`.

## v0.5: Fleet (remote routers and servers)

Link AVRONE servers into a tree. Any server can be a node, a router (a node with
children) or both, and agents on every node can spawn agents on the others.

- **Join:** set `AVRONE_FLEET_TOKEN` (same on all nodes), `AVRONE_NODE_ID`, `AVRONE_PUBLIC_URL`, and `AVRONE_FLEET_PARENT` to the router or root it reports to. It registers and heartbeats; status shows online, degraded or offline.
- **Agent tools:** `fleet_nodes`, `remote_spawn` (target a node, a route like `router1/edge7`, or `auto` for least loaded) and `remote_broadcast` (same task on up to 8 nodes in parallel). Remote agents get the same tools, so they spawn further down the tree.
- **Routing:** jobs are forwarded hop by hop through routers; results return the full path.
- **Security:** every fleet request is HMAC-SHA256 signed (method, path, timestamp, nonce, body hash), 60 s window, replayed nonces rejected, hop limit (`AVRONE_FLEET_MAX_HOPS`, default 3), optional node allowlist (`AVRONE_FLEET_ALLOW`), node-id collision checks. Use HTTPS or a private mesh for confidentiality.
- **Visibility:** `GET /api/fleet` (admin) and the Map tab show the node tree.
- Example: `examples/fleet-compose.yml` (root, router, edge).

## v0.4: Network diagnostics tools

Real, bounded networking from AVRONE's own host (one host and port per call, no port sweeping):

- `dns_lookup`: A/AAAA/MX/TXT/NS/CNAME/SOA/PTR/CAA/SRV via the system resolver plus DNS-over-HTTPS
- `tcp_ping`: TCP connect latency to host:port, no root or ICMP needed
- `traceroute`: hop-by-hop route via mtr, traceroute or tracepath (bundled in the Docker image)
- `rdap_whois`: registrar, dates, nameservers or IP network owner via RDAP
- `ip_geo`: approximate city, ASN and ISP for a public IP or hostname (blank = AVRONE's own)

Ask a squad things like "trace the route to github.com and tell me who runs each network".

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # set your OpenClaw token, or AVRONE_LLM_MODE=mock to demo
avrone                        # http://127.0.0.1:8710
```

### Connect OpenClaw

1. Install and run OpenClaw (the macOS app, or `npm i -g openclaw` on Linux).
2. Turn on its OpenAI-compatible endpoint (off by default). Merge
   `deploy/openclaw.json5` into `~/.openclaw/openclaw.json`, set a token, restart the gateway.
3. Put that token in `AVRONE_LLM_API_KEY`. Default URL is `http://127.0.0.1:18789/v1`.

Each AVRONE agent gets its own OpenClaw session (`x-openclaw-session-key`), so
squad members don't share context.

### Run in Docker

```bash
cp .env.example .env
docker compose up --build
```

AVRONE launches its agent environment as a sibling container through the host
Docker socket, with the workspace bind-mounted.

## API

```bash
# Squad (Grok Heavy style)
curl -s localhost:8710/api/chat -H 'content-type: application/json' \
  -d '{"message":"Build a FastAPI todo app in the workspace and test it","mode":"squad","squad_size":4}'

# Long-running job: returns immediately, watch it on the HUD
curl -s localhost:8710/api/chat -H 'content-type: application/json' \
  -d '{"message":"...","background":true}'

curl -s localhost:8710/api/runs              # past runs
curl -s localhost:8710/api/runs/<run_id>     # full action log
curl -s localhost:8710/api/terminal -H "X-Admin-Token: $AVRONE_ADMIN_TOKEN" \
  -H 'content-type: application/json' -d '{"cmd":"ls -la"}'
# Live events: ws://localhost:8710/ws/events
```

## Safety

Agents run real commands. Prefer the Docker runner, keep `AVRONE_HOST=127.0.0.1`
unless you put auth in front of it, set `AVRONE_ALLOW_NETWORK=false` or
`AVRONE_AGENTS_CAN_SHELL=false` to tighten things, and leave `AVRONE_ADMIN_TOKEN`
empty to turn the admin terminal off.

## Tests

```bash
pytest -q     # runs fully offline with the mock model and local runner
```

## Roadmap

- Native iOS/Android shell on the same API
- Self-hosted runners pool (multiple environments, one per run)
- Model hosting environment (Ollama/vLLM container managed by Sovereign OS)
- Persistent agent memory in Eternal CodeX
