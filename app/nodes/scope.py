"""Source descriptions and the per-source tool view. OWNER: B.

D's registry (app/sources.py) will say which sources exist. Until it lands, the graph takes a plain list of
`Source` records; one adapter function is all that changes when D1 merges.

Why a scoped view: the Phase 1 agent and tool nodes are built from a client's `.tools` and `.call`. Handing
them a view that only lists one source's tools reuses both nodes unchanged and keeps the small model from
seeing every tool at once (design rule 3).
"""
from dataclasses import dataclass
from typing import Any, Sequence

from app.nodes.mcp_client import ToolOutcome

# Pseudo source used by the planner fallback: every tool of every source that is up (Phase 1 behaviour).
ALL_SOURCES = "all"


@dataclass(frozen=True)
class Source:
    name: str
    description: str  # one generic line telling the planner what kind of records live in this source
    tools: tuple[str, ...]  # tool names that read this source (unique across servers, Day 0 decision 2)
    available: bool = True  # False when its server failed to start: recorded as "source unavailable"


class ScopedClient:
    """Looks like McpToolClient to make_agent_node / make_tool_node, but only exposes some tools."""

    def __init__(self, mcp: Any, tool_names: Sequence[str]) -> None:
        allowed = set(tool_names)
        self._mcp = mcp
        self.tools: list[dict[str, Any]] = [t for t in mcp.tools if t["name"] in allowed]
        self._allowed = {t["name"] for t in self.tools}

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        if name not in self._allowed:  # the tool node already refuses these; this is the second lock
            return ToolOutcome(False, error=f"unknown_tool: {name!r}")
        return await self._mcp.call(name, args)


def tool_source_map(sources: Sequence[Source]) -> dict[str, str]:
    """tool name -> the source it reads."""
    return {tool: s.name for s in sources for tool in s.tools}


def unavailable_sources(sources: Sequence[Source], mcp_tools: Sequence[dict[str, Any]]) -> set[str]:
    """Sources that are marked down, or whose tools the live client does not advertise at all."""
    advertised = {t["name"] for t in mcp_tools}
    return {s.name for s in sources if not s.available or not advertised & set(s.tools)}


def scope_tool_names(sources: Sequence[Source], source: str, mcp_tools: Sequence[dict[str, Any]]) -> list[str]:
    """Tool names one sub-question may use. `ALL_SOURCES` = everything except tools of a source that is down."""
    advertised = [t["name"] for t in mcp_tools]
    if source == ALL_SOURCES:
        down = unavailable_sources(sources, mcp_tools)
        blocked = {tool for s in sources if s.name in down for tool in s.tools}
        return [name for name in advertised if name not in blocked]
    wanted = {tool for s in sources if s.name == source for tool in s.tools}
    return [name for name in advertised if name in wanted]
