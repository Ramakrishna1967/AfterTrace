"""Optional MCP v2 read-only proxy (spec p18). Transport only; no business logic duplication.

Exposes the 6 allowlisted read-only diagnostic actions as MCP tools.
execute_approved_plan is intentionally NOT exposed to the diagnostic model.
Authorization, state predicates, approval and executor ownership remain
application responsibilities — a valid MCP schema proves nothing about safety.
"""

from __future__ import annotations

import os

READ_ONLY_ACTIONS = (
    "inspect_alias",
    "inspect_route",
    "verify_target",
    "probe_gateway",
    "inspect_cache",
    "verify_source",
)


def _post_diagnostic(incident_id: str, action: str) -> dict:
    import httpx

    if action not in READ_ONLY_ACTIONS:
        raise ValueError(f"unsupported action {action}")
    # UUID validation at the proxy boundary
    import re as _re

    if not _re.fullmatch(r"[0-9a-fA-F-]{8,64}", incident_id or ""):
        raise ValueError("invalid incident_id")
    base = os.environ.get("AFTERTRACE_CONTROL_URL", "http://127.0.0.1:8000")
    token = os.environ.get("AFTERTRACE_DIAGNOSTIC_TOKEN", "")
    with httpx.Client(
        base_url=base, headers={"Authorization": "Bearer " + token}, timeout=10.0
    ) as client:
        r = client.post(f"/v1/incidents/{incident_id}/diagnostics", json={"action": action})
        r.raise_for_status()
        return r.json()


try:
    from mcp.server import MCPServer  # v2 official SDK path (not FastMCP v1)

    mcp = MCPServer("aftertrace-diagnostics")

    @mcp.tool()
    async def inspect_alias(incident_id: str) -> dict:
        """Inspect the current alias for an authorized incident."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "inspect_alias")

    @mcp.tool()
    async def inspect_route(incident_id: str) -> dict:
        """Inspect the serving route tuple for an authorized incident."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "inspect_route")

    @mcp.tool()
    async def verify_target(incident_id: str) -> dict:
        """Exact manifest comparison of the sealed staging target."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "verify_target")

    @mcp.tool()
    async def probe_gateway(incident_id: str) -> dict:
        """Canary probes through the real gateway/cache path."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "probe_gateway")

    @mcp.tool()
    async def inspect_cache(incident_id: str) -> dict:
        """Metadata for authorized canary cache keys only (never dump other tenants)."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "inspect_cache")

    @mcp.tool()
    async def verify_source(incident_id: str) -> dict:
        """Content-addressed source snapshot checks (no arbitrary paths/URLs)."""
        import asyncio

        return await asyncio.to_thread(_post_diagnostic, incident_id, "verify_source")

except Exception:  # SDK not installed — transport disabled, typed tools remain primary
    mcp = None

    async def inspect_alias(incident_id: str) -> dict:  # type: ignore
        raise RuntimeError("MCP SDK not installed; use typed Python tools")
