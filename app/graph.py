"""LangGraph wiring. OWNER: B.

PLANNER_ENABLED=false (the default) builds the Phase 1 graph with exactly the wiring it always had:
    agent -> tool_executor -> agent -> ... -> synthesizer
Loop cap: MAX_AGENT_STEPS bounds TOOL ROUNDS (one round = one agent decision plus the
tool call(s) it triggered). After the last allowed round the agent still gets one more
decision; if it asks for yet another tool, the run ends with status="loop_cap".

PLANNER_ENABLED=true builds the Phase 2 graph:
    planner -> retrieve -> evidence_check -> (retrieve again | next sub-question | synthesizer)
`retrieve` runs one compiled agent/tool loop per source (the Phase 1 nodes, unchanged, scoped to that source's tools).
MAX_AGENT_STEPS is then the cap per sub-question attempt and MAX_TOTAL_STEPS bounds the whole run.
"""
from typing import Sequence

from langgraph.graph import END, START, StateGraph

from app import config
from app.nodes import debug
from app.nodes.agent_node import make_agent_node
from app.nodes.evidence_check import make_evidence_check_node
from app.nodes.llm import LLM
from app.nodes.mcp_client import McpToolClient
from app.nodes.planner import make_planner_node
from app.nodes.retrieve import make_retrieve_node
from app.nodes.scope import ALL_SOURCES, ScopedClient, Source, scope_tool_names, tool_source_map, unavailable_sources
from app.nodes.state import AgentState
from app.nodes.synthesizer import make_synthesizer_node, make_synthesizer_v2_node
from app.nodes.tool_node import make_tool_node
from app.tracing import node_span


def logged(name, fn):
    async def wrapper(state):
        update = await fn(state)
        debug.log_node(name, dict(state), update)
        return update

    return wrapper


def build_phase1_graph(llm: LLM, mcp: McpToolClient, known_ids: set[str] | None):
    graph = StateGraph(AgentState)
    graph.add_node("agent", node_span("agent")(logged("agent", make_agent_node(llm, mcp.tools))))
    graph.add_node("tool_executor", node_span("tool_executor")(logged("tool_executor", make_tool_node(mcp))))
    graph.add_node("synthesizer", node_span("synthesizer")(logged("synthesizer", make_synthesizer_node(llm, known_ids))))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        lambda s: s["route"],
        {"tool": "tool_executor", "agent": "agent", "synthesizer": "synthesizer", "end": END},
    )
    graph.add_conditional_edges(
        "tool_executor",
        lambda s: s["route"],
        {"agent": "agent", "end": END},
    )
    graph.add_edge("synthesizer", END)
    return graph.compile()


def build_inner_graph(llm: LLM, client: ScopedClient):
    """The Phase 1 retrieval loop (agent <-> tool_executor) over a scoped client. No synthesizer: the planned graph
    synthesizes once, over every sub-question. A 'synthesizer' route from the agent just means 'done'."""
    graph = StateGraph(AgentState)
    graph.add_node("agent", node_span("agent")(logged("agent", make_agent_node(llm, client.tools))))
    graph.add_node("tool_executor", node_span("tool_executor")(logged("tool_executor", make_tool_node(client))))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        lambda s: s["route"],
        {"tool": "tool_executor", "agent": "agent", "synthesizer": END, "end": END},
    )
    graph.add_conditional_edges("tool_executor", lambda s: s["route"], {"agent": "agent", "end": END})
    return graph.compile()


def build_planned_graph(llm: LLM, mcp: McpToolClient, known_ids: set[str] | None, sources: Sequence[Source],
                       unchecked_prefixes: frozenset[str] | set[str] = frozenset()):
    down = unavailable_sources(sources, mcp.tools)
    inner = {}
    for scope in [*(s.name for s in sources if s.name not in down), ALL_SOURCES]:
        names = scope_tool_names(sources, scope, mcp.tools)
        if names:  # a scope with no tools cannot run; retrieve reports it as unavailable
            inner[scope] = build_inner_graph(llm, ScopedClient(mcp, names))

    graph = StateGraph(AgentState)
    graph.add_node("planner", node_span("planner")(logged("planner", make_planner_node(llm, sources))))
    graph.add_node("retrieve", node_span("retrieve")(
        logged("retrieve", make_retrieve_node(inner, down, tool_source_map(sources)))))
    graph.add_node("evidence_check", node_span("evidence_check")(
        logged("evidence_check", make_evidence_check_node(llm, down))))
    graph.add_node("synthesizer", node_span("synthesizer")(
        logged("synthesizer", make_synthesizer_v2_node(llm, known_ids, unchecked_prefixes))))

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "retrieve")
    graph.add_conditional_edges("retrieve", lambda s: s["route"], {"check": "evidence_check", "end": END})
    graph.add_conditional_edges(
        "evidence_check",
        lambda s: s["route"],
        {"retrieve": "retrieve", "synthesizer": "synthesizer", "end": END},
    )
    graph.add_edge("synthesizer", END)
    return graph.compile()


def build_graph(llm: LLM, mcp: McpToolClient, known_ids: set[str] | None, sources: Sequence[Source] | None = None,
                unchecked_prefixes: frozenset[str] | set[str] = frozenset()):
    """`sources` is the registry (D). It and `unchecked_prefixes` are ignored when the planner is off, so that
    path cannot change."""
    if not config.PLANNER_ENABLED:
        return build_phase1_graph(llm, mcp, known_ids)
    return build_planned_graph(llm, mcp, known_ids, sources or (), unchecked_prefixes)
