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
| `OLLAMA_HOST` | Ollama API. Default `http://localhost:11434`. If nothing answers, rounds and the header say **Ollama unreachable**. |
| `OLLAMA_MODEL` | Model name for an advisory round. Defaults to the first model returned by `/api/tags`. |
| `CROW_REPO` | Checkout to ingest. Defaults to the parent of this directory when it contains `.git`. |
| `CROW_PORT` | Listen port. Default `8765`. |
| `CROW_HOST` | Listen address. Default `127.0.0.1`. |

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
- **Ollama advice** needs a reachable `OLLAMA_HOST` with a model installed. Unreachable is a failed advisory round, not a guessed result.
- Sibling services (CycleKernal, capability-authority, the fusion bridge, avrone-daemon) are outside this repository and are not shown as live status.

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
