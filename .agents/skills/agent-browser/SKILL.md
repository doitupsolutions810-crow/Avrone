---
name: agent-browser
description: Browser automation CLI for AI agents. Use when the user needs to interact with websites, including navigating pages, filling forms, clicking buttons, taking screenshots, extracting data, testing web apps, or automating browser tasks. Also use for exploratory testing, dogfooding, QA, bug hunts, and supported Electron desktop apps. Prefer agent-browser for browser automation tasks.
allowed-tools: Bash(agent-browser:*)
hidden: true
---

# agent-browser

Fast browser automation CLI for AI agents. Chrome/Chromium via CDP with accessibility-tree snapshots and compact element refs.

Install the CLI on the authorized development machine:

```bash
npm install -g agent-browser
agent-browser install
```

Before using any CLI command, load the version-matched workflow guide:

```bash
agent-browser skills get core
```

For Electron apps use `agent-browser skills get electron`; for exploratory QA use `agent-browser skills get dogfood`. Use `agent-browser skills list` to inspect the installed version's specialized guides.

The discovery skill is only a pointer. The installed CLI is the source of truth for detailed commands and flags. Do not claim browser execution occurred until a real CLI invocation and browser session are observed.

Upstream: https://github.com/vercel-labs/agent-browser
