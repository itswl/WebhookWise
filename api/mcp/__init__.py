"""Read-only MCP (Model Context Protocol) server for WebhookWise.

Exposes WebhookWise's existing read-side query layer (decision traces, alert
summaries, overview stats, dead letters, forward-rule ROI) as MCP tools so any
MCP-compatible agent (e.g. an OpenOcta / Claude / Cursor client) can query
WebhookWise directly instead of WebhookWise having to push into each agent.

Design notes:
- Transport is Streamable HTTP (the current MCP remote-transport standard),
  mounted on the main FastAPI app at ``/mcp`` — no separate process, in line
  with the modular-monolith decision.
- The server is intentionally **read-only**. Write/action tools (create silence,
  requeue delivery, trigger reanalysis) are deliberately out of scope for v1;
  they need a real allow/ask/deny + approval gate before an agent can trigger
  side effects.
- Auth reuses the management API key via an ASGI middleware, because the mounted
  Streamable-HTTP app is not a FastAPI route and cannot use ``Depends``.
"""

from api.mcp.server import build_mcp_app, mcp_server

__all__ = ["build_mcp_app", "mcp_server"]
