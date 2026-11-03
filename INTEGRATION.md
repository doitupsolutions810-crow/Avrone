# AVRONE × Grok Bot

This checkout combines the current AVRONE Python runtime with Grok Bot's Node console, tools, browser computer and orchestration. There is one Python runtime: Grok Bot's older vendored `avrone/` package is deliberately excluded.

The exact upstream revisions, imported-file SHA-256 hashes and local changes are recorded in [`avrone/static/source-registry.json`](avrone/static/source-registry.json). Check those resources with `python3 scripts/verify-source-registry.py`.

## Start and open

On the Mac or another Docker host, from this checkout:

```sh
python3 scripts/grok-stack.py check
python3 scripts/grok-stack.py up --open
```

The launcher initializes two separate credentials in the ignored, mode-0600 `.env.grok`, validates Compose, builds both applications and waits for service health. First boot downloads real Ollama models and browser images. It never substitutes a mock model. Existing credentials are preserved.

AVRONE opens at <http://127.0.0.1:8710/integration>; Grok Bot opens at <http://127.0.0.1:3000>. Enter `AVRONE_ADMIN_TOKEN` from `.env.grok` in AVRONE's admin field. The Grok console's existing local API remains available only on the host's loopback port.

Set `LLM_MODEL` and `MODELS_REQUIRED` in `.env.grok` before startup to use a different Ollama model. Both runtimes use the same Ollama service and persistent model volume. The default is `qwen2.5:3b`; a larger model is an operator choice, not a claim of stronger measured results.

```sh
python3 scripts/grok-stack.py verify
python3 scripts/grok-stack.py verify --task 'Your complete task and success criteria'
python3 scripts/grok-stack.py down
```

`down` preserves named volumes. Run data, memory, model files and browser profiles persist in Docker volumes. The existing Mac autopilot remains available for source-based, test-gated self-evolution. Self-evolution inside the combined containers has not been validated; the runtime image does not contain a writable source checkout or Git history.

## What the council does

1. AVRONE runs its real planner, parallel squad and Lead while the separate Grok researcher investigates the same task.
2. The Grok bridge uses a fixed research identity and a restricted, read-only toolset. It cannot fall back to the terminal-enabled main agent or delegate recursively into AVRONE.
3. Both findings enter the same learning bridge. A second Lead compares them and posts a conclusion referring to both findings.
4. A durable receipt binds the reports, upstream source revisions, local implementation registry and final answer. `COMPLETE` means both executions and the final merge completed; it does not prove every report claim. Missing services, failed workers or a failed merge produce `HOLD`.

The reverse direction remains available as Grok Bot's `avrone.squad` tool, now forwarding AVRONE's configured admin credential. AVRONE's `grokbot.ask` tool returns a correlated Grok receipt, with answer-digest validation. Both adapters block redirects and cap response bytes.

The browser sidecar now exposes an actual persistent CDP endpoint with its required Playwright dependency. Its debugging interface is confined to the Compose network.

## Inspectable API

| Route | Meaning |
| --- | --- |
| `GET /api/integration` | Model and Grok reachability, live agent state, tool count |
| `POST /api/integration/conclude` | `{ "message": "...", "squad_size": 2 }`; executes the council |
| `GET /api/integration/receipts/{run_id}` | Retrieves a receipt after checking its digest |
| `GET /api/runs/{run_id}/bridge` | Shared findings and conclusion with references |
| `GET /api/runs/{run_id}` | AVRONE event replay |
| `POST /api/bridge/research` on Grok | Internal, bearer-authenticated researcher endpoint |

AVRONE routes retain the current local/admin policy. The combined Compose profile requires admin authentication across its private service network. A model-list response proves reachability only; successful inference is recorded by an actual council run. SHA-256 digests provide integrity checks, not signatures.

## Validation

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q --cov=avrone --cov-report=json --cov-report=xml
npm --prefix integrations/grok-bot test
npm --prefix integrations/grok-bot run check
python3 scripts/verify-source-registry.py
```

The repository CI runs the Python matrix, Node contracts and Compose configuration check. Real Docker image builds, browser-container lifecycle and model-driven bridge execution remain separate live gates. See `INTEGRATION_VALIDATION.md` for results observed during this integration.
