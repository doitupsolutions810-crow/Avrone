# Crow-OS AVRONE Workspace

Self-hosted coordination board for the ongoing AVRONE workflow: run longer, more robust edge-case rounds, keep the evidence, and fix along the way.

The board reads **this Avrone checkout**. It does not ship sample rounds.

## Start

```bash
python crow-os-workspace/run.py
```

Then open http://127.0.0.1:8765.

From `crow-os-workspace/` the same command is `python run.py`.

Optional environment:

| Variable | Role |
|---|---|
| `GITHUB_TOKEN` or `GH_TOKEN` | Load pull requests and GitHub check runs. Without it, the UI says the token is missing and still shows local git data. |
| `OLLAMA_HOST` | Ollama HTTP API. Default `http://localhost:11434`. The server also looks for an `ollama` binary on `PATH`. If the API does not answer, the UI says **Ollama unreachable** and names whether the binary was found. |
| `OLLAMA_MODEL` | Model name for an advisory round. Chat agents use the model saved on the agent, otherwise the first model from `/api/tags`. |
| `CURSOR_API_KEY` | Optional Cursor cloud-agent backend (`POST /v1/agents`). Without it, that backend says the key is missing and does not call the API. |
| `XAI_API_KEY` | Optional Grok backend (`POST https://api.x.ai/v1/chat/completions`). Without it, Grok says the key is missing. |
| `XAI_MODEL` | Grok model id. Default `grok-4.7`. |
| `CROW_REPO` | Checkout to ingest. Defaults to the parent of this directory when it contains `.git`. |
| `CROW_PORT` | Listen port. Default `8765`. |
| `CROW_HOST` | Listen address. Default `127.0.0.1`. |
| `CROW_OPEN_BROWSER` | Set to `0` to skip opening a browser. Default opens `http://127.0.0.1:8765`. |

On startup the server reads host toolchain versions, repo-local git config, `.env` / `pyproject.toml` when present, and Ollama's `/api/tags`. Token-like values are masked before they are stored or shown. Runs inherit the host process environment; the System tab shows the masked view.

## Chat

The Chat tab is the front door: agents and threads on the left, the transcript in the center, and tool activity plus a live log on the right. Coordination views (branches, pull requests, runs, gates, and the rest) stay available as tabs.

A local Ollama agent gets a tool loop confined to a detached git worktree on a `crow-os/chat-…` branch. It can list, read, and write files, commit a patch, and run pytest with no harness timeout. The patch is registered as a change that stays **unmerged** and **unverified** until the same acceptance gates pass. Cancel stops the turn. Settings lists installed models, sets a default model per agent, and can pull a model through `POST /api/pull`.

Cursor and xAI agents use the same chat. They are skipped, with the reason on screen, when their keys are absent.

## Docker

From the repository root:

```bash
docker compose -f crow-os-workspace/docker-compose.yml up --build
```

The published port is `127.0.0.1:8765`. Inside the container the server binds `0.0.0.0` so that mapping works. The repo is mounted at `/repo`. Host Ollama is expected at `host.docker.internal:11434`.

Install the Python packages if they are not already present:

```bash
python -m pip install -r crow-os-workspace/requirements.txt
```

## What is real

- Branches, commit subjects, and ahead/behind counts come from `git for-each-ref` and `git log`.
- Test files and evidence/JSON artifacts are git blobs. The SHA-256 is computed here from those bytes. Syntax errors are real `compile()` failures, including `avrone-a12lr/tests/test_avrone_abyss_edge.py` on `origin/avrone-a12lr-daemon` when that ref is present.
- Deep ingest covers the current branch, `main`, `origin/main`, `origin/avrone-a12lr-daemon`, `origin/edge/*`, and `origin/squad/round*`. Other branches are listed from git but their trees are not hashed until a refresh that includes them (open PR heads are included once a token is set and the ref exists locally).
- A Python round checks out the chosen commit with `git worktree add --detach` under `/tmp/crow-os-worktrees` and runs `pytest` or the script. The main worktree is not checked out. There is no harness timeout. **Cancel round** signals the process group.
- Round inputs, environment, commit SHA, logs, and hashes stay on the round row. Resume only continues files that did not finish. A sealed pass/fail round cannot be resumed; launch another round to re-verify.
- Ollama rounds call `GET /api/tags` and, when a model is present, `POST /api/generate`. The text is stored as advisory. It cannot satisfy the suite gate.

## What stays dark without extra services

- **Pull requests and CI checks** need `GITHUB_TOKEN` or `GH_TOKEN`. The app will not read credentials out of the git remote URL.
- **Ollama** needs a reachable `OLLAMA_HOST` (default `http://localhost:11434`) and a pulled model. A missing binary or a refused connection is shown as unreachable. Advisory text and chat replies are not invented.
- **Cursor cloud agents** need `CURSOR_API_KEY`. **Grok** needs `XAI_API_KEY`.
- Sibling services (CycleKernal, capability-authority, the fusion bridge, avrone-daemon) live outside this repository. `GET /api/supervisor` reports the built-in supervisor when it is running on 127.0.0.1; a stopped supervisor is `unreachable` and does not invent their status.

## Status rule

Chips used everywhere: `verified`, `unverified`, `blocked`, `draft`, `open`, `failed`, `unmerged`, `merged`.

`verified` is computed. It appears only when every acceptance gate passes:

1. Every acceptance file compiles.
2. Recorded failures are cleared by a later passing Python run. Linking a commit or PR does not clear one.
3. One local Python acceptance round on the current commit passes every acceptance file.
4. That round's log and per-file logs have SHA-256 hashes.
5. When GitHub checks were loaded for a pull request, those checks are green.
6. The operator has attested the evidence.

The **Record verification** button calls the same check and returns HTTP 409 with the blocking reasons otherwise. Automatic gates have no toggle. A change that GitHub has not merged stays `unmerged` even after it is verified. A commit already contained in `main` shows `merged` for that git fact, and still stays `unverified` until the gates pass.

A SyntaxError or a crashing test is stored with its trace, timestamped in the anomaly log, and opened as a fix item. The next required gate stays on that failure until a re-run passes.

## Tests

```bash
cd crow-os-workspace
python -m pytest
```

Covers ingestion hashes, the verification gate, resume, comparison, cancel, and the fix loop.

SQLite and logs live in `crow-os-workspace/var/` (gitignored). The append-only event log is the `events` table.
