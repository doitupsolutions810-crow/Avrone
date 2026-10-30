---
name: privileged-dispatch-gateway
description: Configure and operate AVRONE's deny-by-default privileged fleet dispatch with pairwise peer authentication, bounded capabilities, auditing and safe rollback.
---
1. Confirm the node's own runtime, fleet node id and allowlist before changing privileged configuration.
2. Keep `AVRONE_FLEET_PRIVILEGED` unset or deny by default until an operator explicitly approves the peer relationship.
3. Use a unique random secret of at least 32 UTF-8 bytes for each direct peer relationship. Store it in a secret manager, never in Git, agent memory, logs or a checked-in env file.
4. Configure `AVRONE_PRIVILEGED_PEER_KEYS` as JSON. The sender maps the direct target node id to the secret; the receiver maps the sender node id to the same secret. The value must differ from the shared `AVRONE_FLEET_TOKEN`.
5. Add the peer to `AVRONE_PRIVILEGED_NODES` only on the receiving node, and retain the ordinary `AVRONE_FLEET_ALLOW` check and a one-hop privileged ceiling unless an operator explicitly approves another topology.
6. Keep `AVRONE_PRIV_SHELL_ALLOW` empty unless each bare executable is reviewed for the authority of that node's runner. Prefer read-only, narrowly scoped commands; never assume the gateway itself creates a safe OS sandbox.
7. `priv_shell` only accepts one bounded, allowlisted command with a fixed timeout; `priv_admin` exposes a small read-only action set. Neither grants general command composition, arbitrary code execution or Docker-socket access.
8. Verify both positive and negative paths: valid pairwise request accepted; missing, wrong-peer and altered-body signatures refused; unlisted sender refused; replay refused; every decision audited.
9. Remember that “privileged” means the separately granted AVRONE capability, not automatically host root. Never add `--privileged`, Linux capabilities, host mounts or a Docker socket to make the tier appear enabled.
10. To revoke a peer, remove its id from `AVRONE_PRIVILEGED_NODES`, remove its pairwise key from the local secret mapping, restart/reload the node, and verify a formerly valid request is refused.
11. Record the approved peer ids, capability set, hop bound and verification results without recording any secret value.
