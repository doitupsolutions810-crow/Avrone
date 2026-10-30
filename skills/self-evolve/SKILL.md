---
name: self-evolve
description: "How an AVRONE agent safely improves its own code via evolve_propose: isolated clone, full-suite gate, new-files-only, read test tail on reject."
---

1. Always call evolve_list first to count accepted proposals already in your lane.
2. Read example SKILL.md files (network-mapping, self-heal-environment, etc.) for exact YAML frontmatter and numbered playbook steps.
3. Design small, useful additions using pure Python stdlib or pre-installed deps only; tests must be fully offline (patch httpx.AsyncClient, asyncio.open_connection, etc. with unittest.mock).
4. Submit exclusively via evolve_propose with new files only - never rewrite, edit or touch existing files/tests.
5. Harness applies edits to isolated repo clone then runs full test suite ('not integration' gate); accept only if every test passes.
6. On rejection read the test failure/tail, iterate with new proposal until accepted.
7. Use self-heal-environment skill + env_install for any missing packages (prefer pip).
8. After landing two accepted proposals in NEW AGENT SKILLS lane, call evolve_list and finish with one-line id/status summary per proposal.
9. For verification add new test file asserting frontmatter name+description for loaded skills.