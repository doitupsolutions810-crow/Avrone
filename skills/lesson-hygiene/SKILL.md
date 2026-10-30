---
name: lesson-hygiene
description: Keep the recursive learning loop honest across runs - recall what earlier runs learned, re-check it against the code, retract lessons that turned out wrong, and close open questions on the run bridge before you conclude.
---
1. lesson_recall with your task's keywords before long work. Lessons are what earlier runs concluded
   or noted; the best keyword matches are already injected into your prompt when AVRONE_LESSONS is on,
   so recall again only when your work moves to a new topic.
2. Treat each lesson as a claim, not a fact. Before you build on one, check it the cheapest real way
   (`read_file`, a test run in `shell`, `inspect_tool`). A lesson that names a file, flag or tool that
   no longer exists is stale.
3. lesson_forget a lesson you proved wrong, with its key from lesson_recall and a one-line reason. The
   reason is posted to the run bridge as a correction, so siblings stop relying on it too, and the
   retraction is audited. Never forget a lesson only because it is old; the store already evicts the
   oldest past AVRONE_LESSONS_MAX.
4. research_note the corrected fact as a finding (or a conclusion once it is settled) so the next run
   learns the right version. It reports "already known" when the same text is stored.
5. bridge_status before you finish. It lists questions on this run's bridge that nobody answered:
   answer each with bridge_reply (refs = its id) or say plainly in your final answer that it is open.
6. Conclude with bridge_reply kind conclusion referencing the findings it settles. Conclusions are what
   the run distills into lessons, so write them as checkable statements, not opinions.
