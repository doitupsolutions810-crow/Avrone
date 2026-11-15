# AVRONE blackboard hostile truthiness hardening — evidence registry
Date: 2026-11-15
Target: [AVRONE PR #26](https://github.com/doitupsolutions810-crow/Avrone/pull/26)
Baseline head: 0961bdd396e5f380e82bbb90e314d69455fe7de8

## Defect and mitigation register

| Resource | What is established | Status |
| --- | --- | --- |
| [Bridge implementation](../../avrone/bridge.py) | Eliminate untrusted truthiness before kind, agent, role, node, run-id and origin sanitization; convert str subclasses to base str and preserve raw string identity digest | Source change committed |
| [Hostile truthiness regressions](../../tests/test_bridge_hostile_truthiness.py) | 25 parametrized cases plus 300 deterministic mixed metadata samples; persistence, remote merge, Unicode identity alias, zero-default compatibility | Tests committed; hosted execution pending |
| [Prior Fleet merge accounting](fleet-merge-accounting-2026-11-15.md) | Real merge counts and poisoned health integers | Prior GitHub commit |
| [Fast Bridge CI](../../.github/workflows/bridge-fast.yml) | Python 3.11 / 3.12 targeted suite includes tests/test_bridge_*.py | CI configuration observed |
| [Full CI](../../.github/workflows/ci.yml) | Full-history checkout and pytest matrix | CI configuration observed |
| [PR checks](https://github.com/doitupsolutions810-crow/Avrone/pull/26/checks) | Exact current head's CI must pass before promotion | Pending |

A standalone reproduction of the proposed normalization helpers passed **18/18 local checks**, including 300 seeded inputs. Source regression module was separately syntax-compiled; the complete AVRONE Python package was not locally cloned or tested due unavailable github.com DNS. Do not interpret isolated passes as package, swarm, or deployment certification.

Release decision: **HOLD**, no merge or deployment.


## Additional identity collision regression

A non-string object whose __str__ returns a lone surrogate previously had its
spelling normalized before the digest was computed. This could alias a valid
Unicode run ID after sanitization. The follow-up patch preserves the raw
unsanitized spelling until the collision-resistant hash is produced, while
still forbidding hostile __bool__ and overridden str subclass methods.

- [New regression](../../tests/test_bridge_hostile_truthiness.py):
  test_nonstr_surrogate_run_id_cannot_alias_normalized_unicode.
- [Bridge identity normalization](../../avrone/bridge.py): bridge_run_id,
  safe_run_id and _blackboard_run_id.
- Isolated helper check: 19/19 passing including this additional case.
- Hosted full-package CI remains required and unverified.
