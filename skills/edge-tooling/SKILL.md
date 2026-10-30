---
name: edge-tooling
description: Playbook for building new bounded agentic tools.
---

1. Survey existing tools, plugins and skills (list_files, read_file, skill_list, skill_load) to find a small bounded gap.
2. Implement as pure-Python new file only (no rewriting existing); follow exact SKILL.md frontmatter if a skill.
3. Ensure fully offline tests: use unittest.mock.patch on httpx.AsyncClient, asyncio.open_connection and similar.
4. Add new test file (e.g. test_skills_r4.py) asserting both skills load with correct name and description from frontmatter.
5. Submit with evolve_propose; use test_cmd if necessary to keep the 'not integration' gate passing.
6. On reject: read the test tail, correct and resubmit (new files only).
7. Bound the tool: explicit limits, auth checks, no unauthorized scans or net in tests.
8. After acceptance call evolve_list, use evolve_diff to review, and skill_load the new item.
9. Integrate self-heal-environment for any missing deps via env_install.