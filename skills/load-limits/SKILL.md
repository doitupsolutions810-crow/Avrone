---
name: load-limits
description: Explain or tune why AVRONE refused a run (HTTP 429 or 413, an MCP busy result, an agent cap) and size the load caps for the host it runs on.
---
1. limits_status shows every cap, its value and whether it came from env or the default, plus live
   usage: model calls in use, background runs in flight, agents working, agents started per run.
2. Match the refusal to its cap: HTTP 429 from /api/chat is AVRONE_MAX_BACKGROUND_RUNS; HTTP 413 or
   "task too long" is AVRONE_MAX_MESSAGE_CHARS; an MCP "busy" result is AVRONE_MCP_MAX_INFLIGHT; a
   spawn refused mid-run is AVRONE_MAX_AGENTS_PER_RUN. A 413 "request body too large" is
   AVRONE_MAX_BODY_BYTES (never set below what the message cap needs); a peer refused with
   "registry full" is AVRONE_FLEET_MAX_NODES (offline peers are evicted first). Waiting for runs to finish clears the first
   and third; the others need a smaller task or a deliberate cap change.
3. Size for the host. Model concurrency (AVRONE_MAX_PARALLEL) is the real throttle: on a CPU-only
   box running a local model through Ollama keep it at 1-2, because parallel calls only queue inside
   the model server; with a GPU or a hosted model raise it. Background runs can sit well above it
   since they wait on the same semaphore.
4. Caps can only be set within their hard ceilings; a value past the ceiling is clamped, junk falls
   back to the default. Never set a cap by editing code.
5. Before raising caps on an exposed node, run exposure_check - more capacity for strangers is not
   the goal. research_note the values chosen and why.
