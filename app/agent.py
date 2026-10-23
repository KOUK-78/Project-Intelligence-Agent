"""Public entry point: run_agent(question) -> AgentResult. OWNER: B.

ALL agent logic is reachable from here; the CLI is a thin wrapper around run_agent.
run_agent never raises: every failure comes back as an AgentResult with status="error".
Tracing: app/runtime.py wraps run_agent in tracing.observe_run (root span, trace_id, run log),
so trace_id is returned as None here and filled in by that wrapper (CONTRACTS.md).
"""
import asyncio
import time
from typing import Any, Literal, NotRequired, Sequence, TypedDict

from app import config
from app.graph import build_graph
from app.nodes.llm import LLM, OllamaLLM, usage_since
from app.nodes.mcp_client import McpStartupError, McpToolClient
from app.nodes.scope import Source
from app.nodes.verify import evidence_ids, load_known_ids

Status = Literal["ok", "loop_cap", "invalid_output", "error"]


class AgentResult(TypedDict):
    """CONTRACT 4. The six required keys are frozen. The three optional keys are the Day 0 additions
    (decision 3); readers must tolerate their absence. They are only present when they have content:
    plan and sources when the planner ran, token_usage when the model reported token counts.

    plan:        one record per sub-question: {"id", "question", "source", "reason" (the planner's),
                 "status" (resolved | unresolved | unavailable | not_run), "outcome" (short code), "steps",
                 "attempts" (1 or 2), "rewritten" (the rewritten query or None), "capped", "unavailable" ([names])}
    sources:     the sources that returned at least one record, in order of first use
    token_usage: {"prompt_tokens", "completion_tokens", "total_tokens", "calls", "by_job": {job: {...}}}
    """

    answer: str
    citations: list[str]
    status: Status
    trace_id: str | None
    tool_calls: list[dict[str, Any]]  # [{"name": str, "args": dict, "latency_ms": int}]
    latency_ms: int
    plan: NotRequired[list[dict[str, Any]]]
    sources: NotRequired[list[str]]
    token_usage: NotRequired[dict[str, Any]]


def plan_records(plan: list[dict], results: list[dict]) -> list[dict[str, Any]]:
    """Join the plan with how each sub-question ended. A sub-question the run never reached is "not_run"."""
    by_id = {r["id"]: r for r in results}
    out = []
    for sq in plan:
        r = by_id.get(sq["id"])
        out.append({
            **sq,
            "status": r["status"] if r else "not_run",
            "outcome": r["reason"] if r else "",
            "steps": r["steps"] if r else 0,
            "attempts": r["attempts"] if r else 0,
            "rewritten": r["rewritten"] if r else None,
            "capped": r["capped"] if r else False,
            "unavailable": list(r["unavailable"]) if r else [],
        })
    return out


def sources_used(evidence: list[dict]) -> list[str]:
    """Sources that returned at least one record (an ID in a successful result), in order of first use."""
    used: list[str] = []
    for e in evidence:
        source = e.get("source")
        if source and source not in used and evidence_ids([e]):
            used.append(source)
    return used


async def arun_agent(
    question: str,
    *,
    llm: LLM | None = None,
    mcp: McpToolClient | None = None,
    known_ids: set[str] | None = None,
    sources: Sequence[Source] | None = None,
) -> AgentResult:
    """Async core. `llm`, `mcp`, `known_ids` and `sources` (the registry) are injectable for tests.
    `sources` only matters when config.PLANNER_ENABLED is true."""
    start = time.perf_counter()
    result: AgentResult = {
        "answer": "",
        "citations": [],
        "status": "error",
        "trace_id": None,
        "tool_calls": [],
        "latency_ms": 0,
    }
    final: dict[str, Any] = {}
    model = llm or OllamaLLM()
    meter = getattr(model, "usage", None)  # OllamaLLM counts tokens; fakes may not
    before = meter.snapshot() if hasattr(meter, "snapshot") else None

    try:
        client = mcp or McpToolClient()
        async with client as mcp_client:
            graph = build_graph(
                model,
                mcp_client,
                known_ids if known_ids is not None else load_known_ids(),
                sources,
            )
            initial: dict[str, Any] = {
                "question": question,
                "messages": [{"role": "user", "content": question}],
                "tool_calls": [],
                "evidence": [],
                "errors": [],
                "warnings": [],
                "steps": 0,
                "consecutive_errors": 0,
                "nudged": False,
                "route": "",
                "status": "",
                "answer": "",
                "citations": [],
            }
            if config.PLANNER_ENABLED:
                initial["sq_results"] = []
            final = await graph.ainvoke(initial, config={"recursion_limit": config.GRAPH_RECURSION_LIMIT})
    except McpStartupError as exc:
        result["answer"] = f"I could not start the tool server, so I can't answer. ({exc})"
    except Exception as exc:  # noqa: BLE001 - run_agent must never raise
        result["answer"] = f"I hit an unexpected error and could not finish. ({type(exc).__name__}: {exc})"[: config.ERROR_TEXT_CHARS]
    else:
        result["answer"] = final.get("answer", "")
        result["citations"] = list(final.get("citations", []))
        result["tool_calls"] = list(final.get("tool_calls", []))
        result["status"] = final.get("status") or ("ok" if result["answer"] else "error")
        if config.PLANNER_ENABLED and final.get("plan"):
            result["plan"] = plan_records(final["plan"], list(final.get("sq_results", [])))
            result["sources"] = sources_used(list(final.get("evidence", [])))

    if before is not None:
        spent = usage_since(before, meter.snapshot())
        if spent["total_tokens"] > 0:  # absent, not zero, when the model reported nothing
            result["token_usage"] = spent

    result["latency_ms"] = int((time.perf_counter() - start) * 1000)
    return result


def run_agent(question: str) -> AgentResult:
    """Sync wrapper for the CLI. From inside a running event loop, `await arun_agent(...)` instead."""
    return asyncio.run(arun_agent(question))
