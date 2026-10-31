# AVRONE VPS Runbook (round 21)

Deploy AVRONE on a persistent, loopback-hardened VPS with a local model, then add a Fly.io
edge node to the fleet. The bootstrap script is `deploy/vps-bootstrap.sh`; this runbook is
the operator's manual around it.

## 1. Verdict and sizing (audited 2026-10-31)

Primary: **your own VPS** (Hetzner class, from ~EUR 5.99/mo for 4 vCPU / 8 GB). Reasons:
Ollama and GPU work only run where you control the host, `data_dir` must be a persistent
volume (the fleet replay cache lives there), and per-GB platforms price the same node at
~$163/mo. Optional second node: Fly.io (~$20/mo) as a fleet edge for offload and warm
presence, not for model hosting.

| Node | vCPU / RAM | Disk | Est. cost | Role |
|---|---|---|---|---|
| VPS (primary) | 4 / 8 GB | 80 GB (data on a separate volume) | ~$14/mo | squad runs, Ollama (qwen2.5:3b), fleet parent |
| Fly.io (optional) | shared-1x / 1 GB | 3 GB volume | ~$20/mo | fleet edge, dispatch, offload target |

## 2. Provision

1. Fresh Ubuntu 22.04/24.04, non-root sudo user, SSH keys only.
2. Attach a persistent volume; mount it (e.g. `/srv/avdata`) and note the path.
3. Set `AV_HOME=/srv/avdata` before running the bootstrap so `AVRONE_DATA_DIR` lands on it.

## 3. Bootstrap

```bash
export AV_HOME=/srv/avdata
sudo mkdir -p "$AV_HOME" && sudo chown -R "$USER" "$AV_HOME"
WITH_OLLAMA=1 bash deploy/vps-bootstrap.sh
```

The script: installs packages (+ Docker), clones and installs `.[dev]`, runs the full
offline gate (`python -m pytest -q`, expected ~2908 passed - **stop and fix before
proceeding if it is not green**), creates `AVRONE_DATA_DIR` on the volume, generates
`.env.production` (mode 600) with `AVRONE_HOST=127.0.0.1` +
`AVRONE_PUBLIC_ACCESS=loopback` and fresh `openssl` tokens, installs a systemd unit
(`avrone.service`), and optionally pulls `qwen2.5:3b` for Ollama with
`OLLAMA_CONTEXT_LENGTH=4096`.

Verify: `curl -s http://127.0.0.1:8787/api/status` from the box itself.

## 4. Harden before any exposure

- Keep loopback + `AVRONE_PUBLIC_ACCESS=loopback` unless you deliberately open up.
- Expose only through a reverse proxy (Caddy/nginx) with TLS; proxy to `127.0.0.1:8787`.
- If you set `AVRONE_PUBLIC_ACCESS=admin` or `all`, verify with `exposure_check` first:
  bind address, token strength, which routes a tokenless caller reaches. Load the
  `exposure-hardening` skill and follow it line by line.
- Never put tokens in the repo, shell history, or run logs; `secret_scan scope=runs` before
  sharing any log.
- Load `skills/deploy-readiness` and produce the go/no-go list before going live.

## 5. Model route

- Default profile: `AVRONE_LLM_MODE=mock` (no keys, safe bring-up).
- Live profile: `AVRONE_PROVIDER=ollama`, `AVRONE_LLM_MODEL=qwen2.5:3b`, and keep a remote
  fallback via `AVRONE_LLM_FALLBACKS` if you have a second provider.
- Re-run the live bench on the new host before trusting it:
  `NO_PROXY=127.0.0.1 python tools/model_bench.py` (expect qwen2.5:3b 100/100 on 4 vCPU).
- On a GPU host, point at a bigger tool-calling model with one env change; re-run the bench.

## 6. Fleet edge (optional Fly.io node)

1. Deploy the same repo on Fly with `AVRONE_FLEET_PARENT=https://<vps-public-url>`.
2. On the VPS, allow the edge node id in `AVRONE_FLEET_ALLOW` (or keep the signed default).
3. On the edge, run `fleet_status` and `peer_audit`; on the VPS, confirm the join in
   `fleet_status`. Load `skills/peer-onboarding` first.
4. Use `AVRONE_FLEET_OFFLOAD=on` on the VPS to send heavy squads to the edge only when its
   governor is healthy (see `skills/edge-node-health`).

## 7. Operations cadence

- **Logs**: weekly `log_health`, then `run_logs prune=true older_than_days=30` after
  `secret_scan`. Load `skills/log-retention`.
- **Backups**: the only state that matters is `AVRONE_DATA_DIR` (runs, fleet registry,
  replay cache, memory) - snapshot the volume nightly; config in `.env.production`
  (tokens!) goes in your secret store, not in git.
- **Upgrades**: `git fetch`, re-run the gate, `systemctl restart avrone`, then
  `state_health` + `exposure_check` after restart (see `skills/fleet-restart-safety`).
- **Watchdogs**: `sudo systemctl status avrone`, `journalctl -u avrone -n 50`,
  `proc_status` and `governor` readings from inside a run.

## 8. Go-live checklist

- [ ] Gate green on the VPS before any config change
- [ ] `AVRONE_DATA_DIR` on the persistent volume, volume in nightly snapshots
- [ ] `.env.production` mode 600, tokens stored, `secret_scan` clean
- [ ] `exposure_check` reviewed; loopback or proxy+TLS, never a raw public bind
- [ ] systemd unit enabled, `Restart=on-failure`, server answers `/api/status`
- [ ] model bench re-run and recorded; fallback chain set
- [ ] `state_health` clean; fleet join verified (if the edge node is used)
