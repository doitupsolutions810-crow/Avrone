---
name: toolset-hardening
description: Use the self-evolve loop to harden AVRONE's own tools - audit the toolset, pick a weak tool, prove the weakness with a failing test, and land the fix through a gated proposal.
---
1. registry_audit for the map: tools by source, acting vs read-only, and the tools with no arg
   schema (the model gets no pre-call ARG ERROR for those). inspect_tool on a candidate shows its
   args and schema.
2. Prove the weakness before fixing it. Write the smallest test that fails today (junk args, a
   missing file, a corrupt state file, a path that leaves the workspace) and keep it with the fix.
   Never change a tool without a test that would have caught the bug.
3. evolve_propose the fix and its test together as edits with a goal naming the weakness. The
   proposal runs the full suite in its own worktree; only a passing suite is accepted.
4. For several alternative fixes, evolve_start with one candidate per alternative; the loop stops
   at the first passing one within its iteration and time caps (AVRONE_EVOLVE_LONG=allow raises
   the caps for long runs). evolve_status shows progress; evolve_cancel stops it cleanly.
5. evolve_diff the accepted proposal and summarize what changed and why. Discard rejected ones
   with evolve_reset.
6. research_note the weakness and the fix as a conclusion so later runs recall it, and
   lesson_forget any earlier lesson the fix proved wrong.
