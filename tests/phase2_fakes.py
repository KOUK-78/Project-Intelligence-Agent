"""Test doubles for Phase 2: a three-source fake MCP client and an LLM fake that records tools and usage.

No Ollama, no GPU, no real servers. The real multi-server client is D's; this one only honours the surface
the graph relies on: async context manager, `.tools`, and `.call(name, args) -> ToolOutcome`.
"""
import json
from typing import Any, Callable

from app.nodes.llm import LLMReply
from app.nodes.mcp_client import ToolOutcome
from app.nodes.scope import Source
from tests.fakes import FakeLLM

SCHEMA = {"type": "object", "properties": {"keyword": {"type": "string"}, "ticket_id": {"type": "string"}}}


def spec(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"fake {name}", "input_schema": SCHEMA}


SOURCES = [
    Source("jira", "issue tracker records", ("search_tickets", "get_ticket", "list_overdue")),
    Source("notion", "wiki pages", ("search_pages", "get_page")),
    Source("email", "mailbox threads", ("search_emails", "get_email", "get_thread")),
]
ALL_TOOLS = [spec(t) for s in SOURCES for t in s.tools]

# Canned results: every record carries an "id" (CONTRACTS.md section 3).
RECORDS = {
    "search_tickets": {"count": 1, "results": [{"id": "ATL-001", "title": "t"}]},
    "get_ticket": {"id": "ATL-001", "title": "t", "comments": []},
    "list_overdue": {"count": 0, "results": []},
    "search_pages": {"count": 1, "results": [{"id": "NTN-001", "title": "p"}]},
    "get_page": {"id": "NTN-001", "title": "p"},
    "search_emails": {"count": 1, "results": [{"id": "EML-001", "subject": "s"}]},
    "get_email": {"id": "EML-001", "subject": "s"},
    "get_thread": {"id": "EML-001", "messages": []},
}


class FakeMcp:
    """Async-context-manager client over several fake sources. `results` overrides RECORDS per tool;
    a value that is a ToolOutcome is returned as is, a callable gets (name, args)."""

    def __init__(self, tools=None, results: dict[str, Any] | None = None) -> None:
        self.tools = tools if tools is not None else list(ALL_TOOLS)
        self.results = {**RECORDS, **(results or {})}
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, args))
        value = self.results.get(name, {"error": "not_found"})
        if callable(value):
            value = value(name, args)
        if isinstance(value, ToolOutcome):
            return value
        return ToolOutcome(True, value, None, 1)


class SpyLLM(FakeLLM):
    """FakeLLM that also records the tool names each call offered, and reports token usage when asked."""

    def __init__(self, usage: dict[str, tuple[int, int]] | None = None, **script) -> None:
        super().__init__(**script)
        self.tools_seen: dict[str, list[list[str]]] = {}
        self._usage = usage or {}

    async def chat(self, job, messages, *, tools=None, format=None) -> LLMReply:
        names = [t["function"]["name"] for t in (tools or [])]
        self.tools_seen.setdefault(job, []).append(names)
        reply = await super().chat(job, messages, tools=tools, format=format)
        prompt, completion = self._usage.get(job, (0, 0))
        reply.prompt_tokens, reply.completion_tokens = prompt, completion
        return reply


def plan_reply(*items: tuple[str, str]) -> LLMReply:
    """items = (source, question) pairs -> a planner JSON reply."""
    subs = [{"id": f"x{i}", "question": q, "source": s, "reason": "r"} for i, (s, q) in enumerate(items, 1)]
    return LLMReply(content=json.dumps({"sub_questions": subs}))


def check_reply(sufficient: bool, missing: str = "") -> LLMReply:
    return LLMReply(content=json.dumps({"sufficient": sufficient, "missing": missing}))


def rewrite_reply(question: str) -> LLMReply:
    return LLMReply(content=json.dumps({"question": question}))


def script_once(replies: list[LLMReply]) -> Callable[[int], LLMReply]:
    return lambda n: replies[min(n, len(replies) - 1)]
