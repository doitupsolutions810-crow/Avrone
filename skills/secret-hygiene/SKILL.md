---
name: secret-hygiene
description: Find and clean secrets that leaked into this node's run logs, data or workspace, and keep new ones out - without ever printing a secret value.
---
1. secret_scan on the run logs (the default) lists run-log lines holding a secret-shaped string: path, line and kind
   (env-secret-value, bearer, api-key, url-credentials, private-key, ...). It never prints values;
   never print them yourself, and never read_file a flagged line back into the conversation.
2. Run logs written before v0.28 may hold secrets; new events are redacted on write
   (AVRONE_CODEX_REDACT stays on). secret_scan with fix set to true rewrites the flagged run
   logs in place and rescans; confirm it reports 0 lines still flagged.
3. The data and workspace scopes are report-only. A hit in the workspace (a script, a config) is
   fixed by moving the value into an environment variable and editing the file; tell the user
   which key to rotate, because a value that was written to disk should be treated as exposed.
4. exposure_check: if run history was ever reachable by others (AVRONE_PUBLIC_ACCESS=all on an
   exposed bind), say so; rotation is then required, not optional.
5. research_note the outcome (counts and kinds only) so the next run knows the logs are clean.
