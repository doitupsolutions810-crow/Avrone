# Working in this repo (Base44 sandbox notes)

AVRONE is one Python/FastAPI process that serves the web UI, REST API and the
HUD WebSocket. There is no separate frontend build: `avrone/static/index.html`
is served as-is.

## Run

```bash
docker compose -f docker-compose.base44.yml up -d
docker compose -f docker-compose.base44.yml logs -f avrone
```

- `avrone` runs the cloned source with `uvicorn --reload` on host port 3000
  (bind-mounted at `/app`, `pip install -e .` on container start, cached in the
  `pip-cache` volume).
- `ollama` is the model backend (`AVRONE_PROVIDER=ollama`,
  `AVRONE_LLM_MODEL=qwen2.5:3b`) so no external credentials are needed.
  `ollama-pull` is a one-shot service that downloads the model before the server
  starts; the first boot therefore takes a few minutes, later boots are fast
  because the model lives in the `ollama-models` volume.
- `avrone` depends on `ollama-pull` completing, so a slow/blocked model download
  keeps the web tier from starting — check `logs ollama-pull` first if port 3000
  stays down.
- Health: `GET /api/status` (no auth). Admin-only routes need the dev token
  `AVRONE_ADMIN_TOKEN=dev-admin` from `.env.base44-defaults` in the
  `X-Admin-Token` header.

## Quirks worth knowing

- **Do not mount the Docker socket.** `AVRONE_RUNNER=local` makes agents run
  commands inside the web container, confined to `/app/workspace` (volume).
  Leaving `auto` would pick Docker only if a `docker` binary exists in the image.
- **CPU inference is slow.** A single agent turn takes ~1.5 min because AVRONE
  sends a large tool/skill prompt (~66 tools registered). Squad mode multiplies
  that by the planner + N workers + lead, so prefer `"mode": "chat"` for quick
  checks and use `"background": true` for squad/deep runs (watch them on the HUD).
- To use a hosted model instead, set `AVRONE_PROVIDER` + `AVRONE_LLM_BASE_URL` +
  `AVRONE_LLM_MODEL` and the provider's key (`XAI_API_KEY`, `OPENAI_API_KEY`,
  `OPENROUTER_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`). `/run/base44/app.env` is
  loaded after `.env.base44-defaults`, so dashboard values win.
- `AVRONE_LLM_MODE=mock` runs a scripted model with no backend at all — handy for
  exercising orchestration/tools without waiting on inference.
- Three files (`pyproject.toml`, `avrone/__init__.py`, `README.md`) arrived with a
  stray `successfully downloaded text file (SHA: …)` line at the top, which broke
  `pip install` and the package import. They were cleaned in the setup commit;
  CI still strips that line defensively.

## Tests

The dev extras are not installed by the runtime image. Run the suite in the
container with:

```bash
docker compose -f docker-compose.base44.yml exec -T avrone \
  sh -ec 'pip install -q -e ".[dev]" && python -m pytest -q'
```

CI (`.github/workflows/ci.yml`) runs the same suite on Python 3.11 and 3.12.
