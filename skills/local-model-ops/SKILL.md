---
name: local-model-ops
description: Run AVRONE on a free local model server (Ollama), check the model route and circuits, pick a model by the live bench rather than by guesswork, and keep a remote fallback chain.
---
1. router_status first. It shows the primary upstream, the AVRONE_LLM_FALLBACKS chain, each circuit
   (closed, open with its cooldown, half-open) and each upstream's route. A local server must read
   route=direct; calls to 127.0.0.1 or localhost never go through an HTTP(S)_PROXY, while remote
   providers read route=env-proxy when one applies. API keys, base URLs and the proxy are never shown.
2. env_report with {"prefix": "AVRONE_LLM"} to confirm what this node runs: AVRONE_PROVIDER=ollama,
   AVRONE_LLM_MODEL (default qwen2.5:3b), AVRONE_LEAD_MODEL for a stronger merge model, and
   AVRONE_LLM_TIMEOUT. Small CPU-only hosts need a generous timeout: a 3B model answers in seconds per
   turn, and a full two-worker squad takes minutes.
3. Pick models by the bench, not by name. `python3 tools/model_bench.py --base http://127.0.0.1:11434
   --models <tag>` scores each model live (no mocks): valid JSON actions every turn, a real tool call
   instead of a guessed value, a two-worker squad with a Lead merge, and a deep review wave. A model that
   prints a tool "result" it never ran is guessing; prefer one that scores the tool stage in full.
4. Watch the host, not just the model. A model the host cannot hold is killed while loading and the
   server answers 500: free memory first or pick a smaller tag. On 2 CPUs and about 8 GB of RAM, 3B
   models fit; 4B multimodal models may not. On a GPU host set AVRONE_LLM_MODEL to a larger Qwen.
5. Keep a fallback. With AVRONE_LLM_FALLBACKS set, a failing local server opens its circuit after
   AVRONE_BREAKER_THRESHOLD failures and calls move to the next upstream for AVRONE_BREAKER_COOLDOWN
   seconds; router_status shows which one is open and when it is retried.
6. Use AVRONE_LLM_MODE=mock only for offline tests: no upstream is called, and router_status says so.
