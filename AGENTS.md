# AGENTS.md — running AVRONE in the Base44 sandbox

## Dev environment

- `docker compose -f docker-compose.base44.yml up -d` runs the FastAPI app straight from the
  bind-mounted source (`uvicorn --reload`, watching `avrone/`). UI + API are on host port 3000
  (container port 8710). The project's own `Dockerfile` bakes the source into an image and is not
  used here.
- Dependencies are installed from `pyproject.toml` on every container start (editable install with
  the `[dev]` extras, pip cache kept in the `avrone-pip-cache` volume). The repo has no
  `requirements.txt` and no lockfile — `pyproject.toml` is the pin.
- Runtime state lives in bind-mounted, gitignored dirs: `data/` (run logs, memory, checkpoints)
  and `workspace/` (agent sandbox). Deleting them resets the app to a clean slate.
- No database: everything is files under `data/`.

## Non-obvious things

- Committed files can carry a file-delivery artifact on line 1
  (`successfully downloaded text file (SHA: ...)`). In `avrone/__init__.py` that is a Python
  syntax error, and in `pyproject.toml` a TOML error, so neither importing the package nor
  installing it works. CI strips the line before installing. If it reappears:
  `grep -rlI '^successfully downloaded text file' . | xargs sed -i '1{/^successfully downloaded text file/d}'`
- Single origin on purpose: the static UI (`avrone/static/index.html`) calls `/api/*` and
  `/ws/events` relative to the page, so no CORS or API-base-URL configuration is needed. Keep it
  on port 3000 rather than splitting the API onto another port.
- `AVRONE_RUNNER=local`: the sandbox has no Docker socket, so agents run as subprocesses inside
  the server container, confined to `workspace/`. `AVRONE_ALLOW_NETWORK` only affects the Docker
  runner, which is not used.
- `.env.base44-defaults` sets `AVRONE_LLM_MODE=mock` (the app's documented demo mode) so the
  preview answers without a model backend. Real answers need `AVRONE_LLM_MODE=live` plus a
  provider and its key (`AVRONE_PROVIDER=xai|openai|openrouter` + the matching `*_API_KEY`).
  Dashboard values win over that file because `/run/base44/app.env` is the last `env_file` entry.
- `AVRONE_ADMIN_TOKEN` is deliberately unset (admin terminal / `/api/terminal` returns 403)
  because the preview URL is public.

## Verify

- `curl localhost:3000/api/status` → runner, provider, model and tool count; `/` serves the UI.
- Chat round-trip:
  `curl -s -X POST localhost:3000/api/chat -H 'content-type: application/json' -d '{"message":"hi","mode":"squad"}'`
- Tests run inside the container:
  `docker compose -f docker-compose.base44.yml exec -T avrone python -m pytest -q`
