# AVRONE service manager

`avrone-svc` supervises Ollama, the AVRONE server, the four squad lanes, and
optional sibling services. It does not call systemd or launchd. Cloud VMs whose
pid 1 is tini can run it in the foreground; it also reaps zombies when it is
pid 1 itself.

## Run

```bash
python -m avrone.svcman check
python -m avrone.svcman run --state-dir ~/.local/state/avrone-svc
python -m avrone.svcman start
python -m avrone.svcman status
python -m avrone.svcman logs avrone -n 100
python -m avrone.svcman tail ollama
python -m avrone.svcman restart avrone
python -m avrone.svcman enable lane-deepcov
python -m avrone.svcman disable lane-deepcov
python -m avrone.svcman reload
python -m avrone.svcman stop avrone
python -m avrone.svcman stop
python -m avrone.svcman units systemd -o /tmp/avrone-units
python -m avrone.svcman units launchd -o /tmp/avrone-units
```

`run` is the process to put under tini or in a pid namespace. `start` launches
`run` in its own session when the supervisor is not already up, then starts
enabled services. `stop` with no names marks services stopped and exits the
supervisor. A SIGTERM to `run` stops child processes and leaves the desired
state as running, so the next `run` brings them back. `kill -9` of the
supervisor leaves children (they are session leaders); the next `run` adopts a
live child whose `AVRONE_SVC_ID` nonce still matches, or starts a new one.

State, logs, the API port, and the bearer token live in the state directory
(`AVRONE_SVC_STATE`, or `~/.local/state/avrone-svc`). The token file is mode
`0600`. The control API binds `127.0.0.1:8799` by default.

```bash
curl -sS -H "Authorization: Bearer $(cat ~/.local/state/avrone-svc/token)" \
  http://127.0.0.1:8799/v1/status
```

Crow-OS reads the same API from `GET /api/supervisor`. A stopped supervisor is
reported as unreachable.

## Definitions

The built-in bundle is `avrone/svcman/bundle/avrone.toml`. Pass `--config` with
a file or a directory of `.toml`, `.yaml`, and `.yml` files. The YAML loader is
the subset documented in `avrone/svcman/yamlmini.py`.

| Service | Command | Port | Notes |
|---|---|---|---|
| ollama | `ollama serve` | 11434 | optional; HTTP `/api/version` |
| avrone | `python -m avrone.server` | 8710 | HTTP `/api/status` |
| lane-deepcov, lane-newmods, lane-tools, lane-robust | `python -m avrone.cli chat` |  | oneshot, disabled until enabled, depend on ollama |
| cyclekernel | `cyclekernel` or `CYCLEKERNAL_BIN` | 8090 | optional; HTTP `/health` |
| capability-authority | `capability-authority` or `CAPABILITY_AUTHORITY_BIN` | 8095 | optional; TCP ready |
| fusion-bridge | `fusion-bridge` or `FUSION_BRIDGE_BIN` |  | optional |
| avrone-daemon | `avrone-daemon` or `AVRONE_DAEMON_BIN` |  | optional |

A missing optional binary becomes `unavailable`. The supervisor keeps running.
Lanes that depend on an unavailable Ollama become `unavailable` as well.

Restart policies are `always`, `on-failure`, and `never`. Backoff is
exponential with jitter. More than `max_restarts` exits inside
`restart_window_seconds` opens a circuit; after `circuit_reset_seconds` one
half-open attempt is allowed. Health checks are HTTP, TCP, or a command.
Dependents wait until a dependency is `ready`. Cycles are refused at load.

Stop sends SIGTERM to the process group, then SIGKILL after `stop_grace_seconds`.
SIGHUP reloads definitions. Logs rotate per service. `events.jsonl` records
start, stop, crash, restart, and anomalies with ISO-8601 UTC timestamps and the
captured traceback tail.

Resource limits use `setrlimit` in the child. cgroup v2 `memory.max`,
`cpu.max`, and `pids.max` are applied when the controller is delegated. If it
is not, the limit is skipped unless `cgroup_required` is set.

`avrone-svc units` writes systemd or launchd files for hosts that have those
managers. Nothing in the supervisor executes them.

## Limitations

- Adopted processes (supervisor was `kill -9`'d) do not reconnect their old
  stdout. New output is captured after the next start.
- The YAML loader is a subset: no anchors, tags, or merge keys.
- Switching `user` requires root. cgroup limits require a delegated v2
  controller; otherwise they are skipped.
- Port checks look for a listener. On Linux that is `/proc/net/tcp` state
  LISTEN, so TIME_WAIT from an old connection does not block a restart. A
  foreign process can still bind in the gap after the check; the child then
  exits and the restart policy applies. When the main process dies, the
  supervisor signals the rest of its process group so a forked child does not
  keep the port.
- Squad lanes are disabled by default. Enabling them runs real `avrone-cli chat`
  jobs against Ollama.
- CycleKernal, capability-authority, the fusion bridge, and avrone-daemon are
  not shipped in this repository. Their definitions look those commands up on
  `PATH` or via the named environment variable.
