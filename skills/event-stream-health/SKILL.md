---
name: event-stream-health
description: Diagnose a live dashboard (HUD on /ws/events) that misses events, lags or piles up connections, without losing any run history.
---
1. stream_status lists every connected subscriber with its queue depth against the 1000-event
   limit, plus events emitted and dropped since start.
2. A subscriber marked FULL is a dashboard that cannot keep up; its missing events are only missing
   from that screen. The run logs hold every event: use codex_search or codex_stats for history.
3. Subscriber count should match the dashboards actually open. Since v0.30 a dropped connection is
   released at once; if the count keeps growing, something is reconnecting in a loop. The stream
   holds at most AVRONE_MAX_WS_SUBSCRIBERS; past it a new dashboard is closed with 1013 and
   stream_status counts it as refused - close idle tabs before raising the cap.
4. Events that arrive late or in bursts (shell output, fleet joins) came from worker threads; since
   v0.33 they wake the stream at once. If they still bunch up, the screen is FULL (step 2), not the
   bus.
5. If the stream should be private, exposure_check confirms /ws/events is not open to remote callers
   without the admin token (AVRONE_PUBLIC_ACCESS).
6. research_note what was seen: subscribers, drops, and the cause found.
