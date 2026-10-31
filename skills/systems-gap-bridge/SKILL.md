---
name: systems-gap-bridge
description: Scan this node's own checkout for coherence gaps (skills, env grounding, version drift, tool wiring, cap mapping), then turn the findings into a long-horizon systems-analysis mission for the squad.
---
1. Run gap_scan with scope all and read the report: skills (broken frontmatter or steps), env (an AVRONE_* a skill names that no code reads), versions (pyproject.toml vs avrone/__init__.py vs the README release heading), wiring (a register_*_tools function nothing calls), caps (a load cap missing from skills/load-limits).
2. Confirm each finding by hand before acting: inspect_tool for the tools it names, registry_audit for registration, read_file on the exact file (the checkout is at AVRONE_REPO_DIR or the workspace).
3. Turn the scan into a mission with gap_mission giving a topic; it returns a bounded brief and logs it to the Eternal CodeX.
4. Emit the brief to the squad: bridge_post it as kind question, then add one task_board card per confirmed finding so the mission has an owner.
5. Cross-exam the findings on the run bridge (bridge_read, bridge_reply); research_note what survives as kind finding so the recursive learning loop picks it up, and lesson_recall before re-reporting what an earlier run already learned.
6. Land fixes only through the gated path: evolve_propose for code changes (throwaway copy, full suite must pass), then re-run gap_scan and record the evidence with great_scale; retract anything that proved wrong with lesson_forget.
