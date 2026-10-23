"""Shared graph state and small helpers. OWNER: B."""
import operator
from typing import Annotated, Any, TypedDict

# NOTE: no `from __future__ import annotations` here: LangGraph resolves these
# Annotated types at runtime.

# Prefix the agent and synthesizer nodes put on `errors` when the language model itself is unreachable.
# The planned graph stops the whole run on it (no other source can help); tool failures only mark a source down.
LLM_UNAVAILABLE = "llm_unavailable"

# Keys added to every evidence item in a planned run (Phase 2). Items keep their Phase 1 keys too.
SQ_KEY = "sq"
SOURCE_KEY = "source"


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
    ref_nudged: bool  # referenced tickets were already read once (system-initiated round)
    auto_round: bool  # the pending calls were issued by the graph, not the model
    capped: bool  # the step cap was hit; the synthesizer answers from partial evidence
    route: str  # next hop: tool | agent | synthesizer | end
    status: str  # "" while running, else ok | loop_cap | invalid_output | error
    answer: str
    citations: list[str]

    # ---- Phase 2 (planner on). Every key below is optional: Phase 1 runs never set them.
    # step_cap overrides config.MAX_AGENT_STEPS for one retrieval attempt (the agent node reads it).
    step_cap: int
    # Validated sub-questions: {"id", "question", "source", "reason"}. Set once by the planner.
    plan: list[dict]
    sq_index: int  # position in `plan` of the sub-question being worked on
    sq_question: str  # query text of the current attempt (the rewrite, on the second attempt)
    sq_attempt: int  # 0 = first retrieval, 1 = retrieval after one rewrite
    sq_steps: int  # tool rounds this sub-question has used, all attempts
    total_steps: int  # tool rounds the whole run has used (checked against MAX_TOTAL_STEPS)
    # How the last retrieval attempt ended: ok | loop_cap | error | unavailable | budget.
    sq_inner_status: str
    # One record per finished sub-question (written by the evidence check).
    sq_results: Annotated[list[dict], operator.add]


def tag_evidence(items: list[dict], sq_id: str, source: str, tool_source: dict[str, str] | None = None) -> list[dict]:
    """Copy evidence items and tag each with its sub-question and source.

    The source comes from the tool when the registry knows it (so a sub-question that ran over all sources
    still credits each result to the right one), else from the sub-question."""
    known = tool_source or {}
    return [{**item, SQ_KEY: sq_id, SOURCE_KEY: known.get(item.get("tool", ""), source)} for item in items]


def evidence_for(evidence: list[dict], sq_id: str) -> list[dict]:
    """The evidence items that belong to one sub-question."""
    return [item for item in evidence if item.get(SQ_KEY) == sq_id]


def fresh_subquestion_state(question: str, step_cap: int) -> dict[str, Any]:
    """Initial state for one retrieval attempt. Everything Phase 1 tracked per run is reset, so the nudge
    flags, step counter, error streak and message history never leak from one sub-question into the next."""
    return {
        "question": question,
        "messages": [{"role": "user", "content": question}],
        "tool_calls": [],
        "evidence": [],
        "errors": [],
        "warnings": [],
        "steps": 0,
        "consecutive_errors": 0,
        "nudged": False,
        "detail_nudged": False,
        "ref_nudged": False,
        "auto_round": False,
        "capped": False,
        "route": "",
        "status": "",
        "answer": "",
        "citations": [],
        "step_cap": step_cap,
    }


def compact_json(value: Any, limit: int) -> str:
    """Short, single-line JSON for feeding to the small model."""
    import json

    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text
