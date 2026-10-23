"""RETRIEVE node (act, once per sub-question attempt): the Phase 1 agent and tool loop, scoped to one source. OWNER: B.

The loop itself is not rebuilt: each source has a compiled graph of the unchanged agent and tool nodes, given only
that source's tools. This node starts it from a fresh state (all Phase 1 flags reset), caps it, and tags what it
found with the sub-question and source. It never decides whether the evidence is enough; the evidence check does.
"""
from typing import Any, Mapping

from app import config
from app.nodes.state import LLM_UNAVAILABLE, AgentState, fresh_subquestion_state, tag_evidence


def attempt_status(final: Mapping[str, Any]) -> str:
    """How an inner run ended: error (tools kept failing) | loop_cap (the step cap stopped it) | ok."""
    if final.get("status") == "error":
        return "error"
    if final.get("status") == "loop_cap" or final.get("capped"):
        return "loop_cap"
    return "ok"


def attempt_cap(total_steps: int) -> int:
    """Tool rounds this attempt may use: the per-sub-question cap, never more than the run has left."""
    return min(config.MAX_AGENT_STEPS, config.MAX_TOTAL_STEPS - total_steps)


def make_retrieve_node(inner: Mapping[str, Any], down: set[str], tool_source: Mapping[str, str]):
    """`inner` maps a source name (or the 'all' scope) to its compiled loop; `down` is the set of sources that are
    not running; `tool_source` maps a tool name to the source it reads (for evidence tags)."""

    async def retrieve_node(state: AgentState) -> dict[str, Any]:
        sq = state["plan"][state["sq_index"]]
        total = state.get("total_steps", 0)

        if sq["source"] in down or sq["source"] not in inner:
            return {"sq_inner_status": "unavailable", "route": "check"}  # no tools to try: say so, do not guess
        cap = attempt_cap(total)
        if cap <= 0:
            return {"sq_inner_status": "budget", "route": "check"}  # budget spent: skipped, not failed

        final = await inner[sq["source"]].ainvoke(
            fresh_subquestion_state(state["sq_question"], cap),
            config={"recursion_limit": config.GRAPH_RECURSION_LIMIT},
        )
        errors = list(final.get("errors", []))
        if any(err.startswith(LLM_UNAVAILABLE) for err in errors):
            # No other source or rewrite can help when the model itself is unreachable.
            return {"route": "end", "status": "error", "answer": final.get("answer", ""), "errors": errors}

        used = int(final.get("steps", 0))
        return {
            "evidence": tag_evidence(final.get("evidence", []), sq["id"], sq["source"], tool_source),
            "tool_calls": list(final.get("tool_calls", [])),
            "errors": errors,
            "sq_steps": state.get("sq_steps", 0) + used,
            "total_steps": total + used,
            "sq_inner_status": attempt_status(final),
            "route": "check",
        }

    return retrieve_node
