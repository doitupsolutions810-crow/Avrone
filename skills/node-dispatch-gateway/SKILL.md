---
name: node-dispatch-gateway
description: Secure multi-node dispatch, bounded tooling, and operator-only host operations for AVRONE.
---

# Secure Node Dispatch Gateway

## Mission
Use the fleet as a bounded execution and collaboration fabric, not as an implicit trust amplifier. A message authenticated by the shared fleet key is authenticated as a peer, not automatically authorized for every possible operation.

## Dispatch protocol
1. Validate every dispatch payload as an object with an explicit mode (chat, squad, or deep), a non-empty bounded prompt, a strict integer squad size, and a bounded route.
2. Enforce both configured hop limits and route uniqueness. Reject malformed IDs, repeated nodes, bool-as-integer values, unbounded messages, and malformed origin_run.
3. Use HTTPS for non-loopback peers or a private authenticated mesh. Treat any insecure-HTTP opt-in as a temporary legacy exception. Never send a fleet key in prompts or logs.
4. Maintain per-node allowlists for registration and dispatch. Shared fleet secrets are trust domains; never join a node you do not control. Plan a per-node identity/key model before multi-tenant fleet hosting.
5. Treat node URLs and persisted node records as untrusted input. Reject URL credentials, unexpected paths, query strings, fragments, invalid ports, and non-loopback plaintext URLs.
6. Bound request size, node registry size, replay-nonce memory, remote run time, retries, and concurrent work. Return clear client errors for invalid payloads; do not silently coerce malformed types.

## Safe tooling and environment
- Do not copy application/provider/fleet credentials into child-process environments, agent prompts, artifacts, or debug output.
- Keep shell-capable agents inside Docker or another dedicated unprivileged sandbox. A workspace directory is a working directory, not a security boundary. Local command timeouts must terminate process groups; Docker commands need an in-container deadline as well as a host-side timeout. Drop Linux capabilities, disable privilege escalation, and bound PID/memory/CPU resources.
- A mounted Docker socket is effectively host-level container administration. Never mount it into agent containers; the coordinator still needs to be treated as a trusted privileged control-plane component. The stock compose container does not control host systemd, so keep the root gateway disabled there unless a separate host-side broker is deployed.
- Keep network access disabled unless the task needs it. Reconnaissance remains explicitly authorized, target-scoped, logged, and allowlisted where practical.
- Plugins and MCP servers can expand effective capabilities; pin and review their sources and arguments, and report the source of every tool.
- Agent-directed fetch/HTTP tools validate schemes, credentials, DNS answers and every redirect. Private RFC1918/IPv6-ULA nodes can be permitted by exact hostname/IP via `AVRONE_HTTP_PRIVATE_ALLOWLIST` when explicitly authorized; wildcards/CIDRs are ignored, and loopback/link-local/reserved/metadata remain blocked. Also strip auth headers on cross-origin redirects, reject redirects on write requests, cap bodies at 1 MB and responses at 4 MB, bound headers and total request time. DNS validation cannot eliminate rebinding; enforce egress with Docker/firewall policy.

## Host operations and privilege
- Never solve a hosting need by giving every agent an unrestricted host-root shell.
- The root gateway is operator-only: it needs the admin token and a separate strong root-operation token, an explicit operation allowlist and an explicit systemd-unit allowlist.
- Only service_status and service_restart are supported. Keep the command argument vector fixed; never accept a shell string, arbitrary executable path, unit file path, sudo flags, or user-provided argv.
- The host must separately enforce least privilege (for example, a root-owned sudoers rule for only the approved systemctl subcommands and units). Application allowlists do not replace OS authorization.
- Keep AVRONE_ROOT_GATEWAY_ENABLED=false until host policy, network exposure, rotation and audit handling have been reviewed. Do not route this endpoint through generic agent tools or expose root tokens to fleet workers.
- Avoid AVRONE_ALLOW_ROOT_LOCAL=true; it is a break-glass bypass for disposable isolated hosts only.

## Self-evolution
- Every change stays in an isolated proposal worktree. Path checks, edit caps, direct-pytest command validation, test results, duration, and final commit are recorded.
- Respect both the per-proposal deadline (AVRONE_EVOLVE_MAX_SECONDS) and the UTC-day reserve budget (AVRONE_EVOLVE_MAX_DAILY_SECONDS, AVRONE_EVOLVE_MAX_DAILY_PROPOSALS). The daily budget reserves worst-case requested time before work starts, so concurrent proposals cannot oversubscribe the configured cap.
- Re-run the full suite if the first full-suite attempt fails; never substitute a last-failed-only subset for the release gate.
- Do not push to the default branch or automatically merge. Human review and a successful exact-head CI run are required for promotion.

## Recon reporting
For every run, state whether the result came from source review, a recorded system log, or live execution. Do not describe source code as current runtime state. Do not scan targets unless the operator has explicitly authorized that target and the configured policy allows it.

- Set `AVRONE_DOCKER_IMAGE_ALLOWLIST` to exact reviewed image references and `AVRONE_DOCKER_REQUIRE_DIGEST=true` for digest pinning. Apply resource ceilings (`AVRONE_DOCKER_MEMORY_LIMIT`, `AVRONE_DOCKER_CPU_LIMIT`, `AVRONE_DOCKER_PIDS_LIMIT`) and run worker containers as the workspace owner. The same policy applies to the primary agent container and temporary docker_run workers.
