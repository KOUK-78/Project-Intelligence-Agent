"""Public entry point: run_agent(question) -> AgentResult. OWNER: B.

ALL agent logic is reachable from here; the CLI is a thin wrapper around run_agent.
run_agent never raises: every failure comes back as an AgentResult with status="error".
Tracing: app/runtime.py wraps run_agent in tracing.observe_run (root span, trace_id, run log),
so trace_id is returned as None here and filled in by that wrapper (CONTRACTS.md).
"""
import asyncio
import time
from typing import Any, Literal, TypedDict

from app import config
from app.graph import build_graph
from app.nodes.mcp_client import McpStartupError, McpToolClient
from app.nodes.llm import LLM, OllamaLLM
from app.nodes.verify import load_known_ids

Status = Literal["ok", "loop_cap", "invalid_output", "error"]


class AgentResult(TypedDict):
    """FROZEN CONTRACT 4. Do not add or rename keys."""

    answer: str
    citations: list[str]
    status: Status
    trace_id: str | None
    tool_calls: list[dict[str, Any]]  # [{"name": str, "args": dict, "latency_ms": int}]
    latency_ms: int


async def arun_agent(
    question: str,
    *,
    llm: LLM | None = None,
    mcp: McpToolClient | None = None,
    known_ids: set[str] | None = None,
) -> AgentResult:
    """Async core. `llm`, `mcp` and `known_ids` are injectable for tests."""
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

    try:
        client = mcp or McpToolClient()
        async with client as mcp_client:
            graph = build_graph(
                llm or OllamaLLM(),
                mcp_client,
                known_ids if known_ids is not None else load_known_ids(),
            )
            final = await graph.ainvoke(
                {
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
                },
                config={"recursion_limit": config.GRAPH_RECURSION_LIMIT},
            )
    except McpStartupError as exc:
        result["answer"] = f"I could not start the Jira tool server, so I can't answer. ({exc})"
    except Exception as exc:  # noqa: BLE001 - run_agent must never raise
        result["answer"] = f"I hit an unexpected error and could not finish. ({type(exc).__name__}: {exc})"[: config.ERROR_TEXT_CHARS]
    else:
        result["answer"] = final.get("answer", "")
        result["citations"] = list(final.get("citations", []))
        result["tool_calls"] = list(final.get("tool_calls", []))
        result["status"] = final.get("status") or ("ok" if result["answer"] else "error")

    result["latency_ms"] = int((time.perf_counter() - start) * 1000)
    return result


def run_agent(question: str) -> AgentResult:
    """Sync wrapper for the CLI. From inside a running event loop, `await arun_agent(...)` instead."""
    return asyncio.run(arun_agent(question))
