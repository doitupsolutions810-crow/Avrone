---
name: agent-comms-and-conclude
description: How agents and their sub-agents talk on the run's shared learning bridge, reach a conclusion together, turn it into lessons the next run recalls, and relay posts between fleet nodes.
---
1. bridge_read first. Every agent of one run (squad workers, their sub-agents at any depth, deep-mode
   reviewers) shares ONE bridge, so read what siblings and your parent already found before you work.
   Filter with kind (finding, question, answer, correction, conclusion), agent, query or since.
2. bridge_post what you learn while you work: kind finding for a fact you verified, question for
   something you need another agent to settle. Keep each post to one claim; the bridge keeps posts up
   to 4000 characters and the newest AVRONE_BRIDGE_MAX_POSTS per run (default 500, hard cap 5000).
3. bridge_reply when you answer, correct or confirm a post: refs must name the post id(s) you reply to
   (shown in brackets by `bridge_read`). Use kind correction when a sibling is wrong and kind conclusion
   when a thread is settled. A reply with no refs is refused.
4. spawn a sub-agent for a separable piece. It joins the same run bridge, so it can read your findings
   and post its conclusion with refs back to them; read the bridge again when it returns rather than
   relying only on its final text.
5. research_note for long-horizon work: it posts to the bridge AND, for a finding or conclusion, stores
   it as a lesson future runs recall. It says whether the note was newly learned or already known, so
   you never record the same lesson twice. Questions are posted only.
6. lesson_recall with a topic before a long task: it returns what earlier runs concluded on related
   work, best keyword match first. The same lessons are injected into new runs automatically
   (AVRONE_LESSONS on, the default); recall them yourself when your task drifts to a new topic.
7. Conclude. When the run ends, its conclusions (then corrections) are distilled into lessons, deduped
   by text hash and capped at AVRONE_LESSONS_MAX. In squad mode with AVRONE_BRIDGE_ROUNDS above 0,
   workers cross-examine each other on the bridge between rounds and stop early once a round adds no
   correction; deep mode's first wave shares the deep run's bridge, so reviewers see what it concluded.
8. Across nodes, bridge posts relay by export and merge: a post keeps its id and origin node, a local
   post is never overwritten, posts the ring already evicted are not brought back, and malformed posts
   are rejected. Check the model side with router_status (provider chain and circuit state) when
   replies stall.
