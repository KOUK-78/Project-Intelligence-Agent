"""Misbehaving MCP server for failure tests. BAD_MODE = slow | malformed | crash."""
import os
import sys

import anyio

MODE = os.environ.get("BAD_MODE", "malformed")


async def _misbehave():
    if MODE == "slow":
        await anyio.sleep(30)
    if MODE == "crash":
        print("bad-server crashing", file=sys.stderr, flush=True)
        os._exit(1)
    return "<<this is not json>>"


async def search_tickets(keyword: str, status: str | None = None, label: str | None = None,
                         overdue: bool | None = None) -> str:
    """Search tickets by keyword."""
    return await _misbehave()


async def get_ticket(ticket_id: str) -> str:
    """Get one ticket by ID."""
    return await _misbehave()


async def list_overdue() -> str:
    """List overdue tickets."""
    return await _misbehave()


def main() -> None:
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server
    server = Server("bad-server")
    for fn in (search_tickets, get_ticket, list_overdue):
        server.tool()(fn)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
