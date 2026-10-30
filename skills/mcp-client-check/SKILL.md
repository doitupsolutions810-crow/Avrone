---
name: mcp-client-check
description: Confirm this node works as an MCP server for an outside client (Claude Desktop, Cursor, another agent) before wiring it up, and diagnose a client that cannot see or call AVRONE's tools.
---
1. mcp_selftest drives the server in-process: initialize, ping, tools/list and three malformed
   requests. PASS means the protocol layer is sound; any FAILED line is a server bug to report.
2. The client launches the avrone-mcp command over stdio. Anything the server prints goes to
   stderr, so a client that shows parse errors is usually running a different command or a wrapper
   that writes to stdout.
3. The four tools are avrone_squad, avrone_chat, avrone_deep and avrone_tools. A client that shows
   none failed the handshake; one that shows them but gets errors on calls usually sends arguments
   that are not an object or a task that is not a string - the server answers those with JSON-RPC
   -32602 or an isError result naming the field.
4. router_status: calls succeed at the protocol level but answer badly when the model route is
   down or the circuit is open; fix the route, not the MCP setup.
5. research_note what was checked and the result.
