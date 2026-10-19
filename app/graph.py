"""LangGraph wiring: agent -> tool_executor -> agent -> ... -> synthesizer. OWNER: B.

Loop cap: MAX_AGENT_STEPS bounds TOOL ROUNDS (one round = one agent decision plus the
tool call(s) it triggered). After the last allowed round the agent still gets one more
decision; if it asks for yet another tool, the run ends with status="loop_cap".
"""
from langgraph.graph import END, START, StateGraph

from app.nodes.agent_node import make_agent_node
from app.nodes.llm import LLM
from app.nodes.mcp_client import McpToolClient
from app.nodes.state import AgentState
from app.nodes.synthesizer import make_synthesizer_node
from app.nodes.tool_node import make_tool_node
from app.tracing import node_span


def build_graph(llm: LLM, mcp: McpToolClient, known_ids: set[str] | None):
    graph = StateGraph(AgentState)
    graph.add_node("agent", node_span("agent")(make_agent_node(llm, mcp.tools)))
    graph.add_node("tool_executor", node_span("tool_executor")(make_tool_node(mcp)))
    graph.add_node("synthesizer", node_span("synthesizer")(make_synthesizer_node(llm, known_ids)))

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
