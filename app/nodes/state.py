"""Shared graph state and small helpers. OWNER: B."""
import operator
from typing import Annotated, Any, TypedDict

# NOTE: no `from __future__ import annotations` here: LangGraph resolves these
# Annotated types at runtime.


class AgentState(TypedDict, total=False):
    question: str
    # Chat history for the agent LLM, excluding the system prompt (added per call).
    messages: Annotated[list[dict], operator.add]
    # Tool calls the agent wants executed next (set by agent, consumed by tool executor).
    pending_calls: list[dict]
    # Frozen-contract record of executed calls: {"name", "args", "latency_ms"}.
    tool_calls: Annotated[list[dict], operator.add]
    # What the tools returned: {"tool", "args", "ok", "data", "error"}.
    evidence: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    steps: int  # completed tool rounds (agent decision + tool execution)
    consecutive_errors: int  # tool rounds in a row where every call failed
    nudged: bool  # agent was already told once to use a tool
    detail_nudged: bool  # agent was already told once to read full tickets
    ref_nudged: bool  # agent was already told once to read referenced tickets
    route: str  # next hop: tool | agent | synthesizer | end
    status: str  # "" while running, else ok | loop_cap | invalid_output | error
    answer: str
    citations: list[str]


def compact_json(value: Any, limit: int) -> str:
    """Short, single-line JSON for feeding to the small model."""
    import json

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text
