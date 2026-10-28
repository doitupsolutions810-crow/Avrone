---
name: release-verification
description: Verify AVRONE releases, test gates, source bundles, version metadata, and security exclusions before promotion.
---

# Release verification

1. Confirm the intended base and head commits. Inspect the complete diff and ensure the release notes describe only changes actually present.
2. Check that pyproject.toml and avrone/__init__.py agree on the package version. Confirm README release notes and skills/project-doc/SKILL.md reflect the actual implementation.
3. Run python -m pytest -q on the exact candidate commit. Confirm the process ran on a real runner, record Python version, test count, exit status, and failures. A missing runner, skipped job, empty logs, or unavailable shell is NOT a pass.
4. Keep unit tests offline. Confirm network-dependent tests are explicitly marked integration or use deterministic HTTP/socket mocks.
5. Build avrone.zip from the reviewed commit. Inspect its file list and ensure .env, tokens, local data/, workspace/, .git/, caches, and generated secrets are excluded.
6. Verify the artifact exists, its commit SHA matches the tested commit, it can be opened, and it contains the expected source tree. Do not claim an artifact exists based only on workflow configuration.
7. Check container/deployment instructions, migrations, environment variable defaults, rollback notes, and CI status before recommending promotion.
8. Keep the PR unmerged when tests cannot execute or release evidence is incomplete. Record blockers and exact links to the branch, commit, and workflow run.

## AVRONE release-specific safety

- Recon/OSINT stays bounded to named infrastructure, gated by the current policy and optional allowlist.
- No credentials or sensitive runtime files belong in the source bundle.
- No claim of satellite, cellular, private-network, or quantum access without a concrete, verified integration.
- A green test suite proves only the tested code and conditions; it does not establish capabilities that were not exercised.
