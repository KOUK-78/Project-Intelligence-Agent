"""Follow-ups from D's review: source-neutral startup text and a reference round that stays inside its own source."""
import asyncio
from pathlib import Path

from app.agent import arun_agent
from app.nodes.agent_node import unread_references
from app.nodes.mcp_client import McpToolClient
from tests.fakes import FakeLLM, text_reply

HERE = Path(__file__).parent


def read_ticket(text: str) -> dict:
    data = {"id": "ATL-112", "title": "t", "comments": [{"author": "a", "date": "2026-06-01", "text": text}]}
    return {"tool": "get_ticket", "args": {"ticket_id": "ATL-112"}, "ok": True, "data": data, "error": None}


def state(evidence, question="What does this depend on?"):
    return {"question": question, "evidence": evidence}


def test_reference_round_follows_ticket_ids_only():
    ev = [read_ticket("waiting on ATL-118, see page NTN-004 and the thread EML-009")]
    assert unread_references(state(ev)) == ["ATL-118"]


def test_reference_round_ignores_other_sources_even_when_a_ticket_only_names_them():
    assert unread_references(state([read_ticket("see page NTN-004 and EML-009")])) == []


def test_reference_round_is_unchanged_for_plain_ticket_references():
    ev = [read_ticket("blocked by ATL-118 and ATL-120")]
    assert unread_references(state(ev)) == ["ATL-118", "ATL-120"]
    assert unread_references(state(ev, question="What is the status?")) == []  # not a dependency question
    assert unread_references(state([])) == []  # nothing read yet


def test_startup_failure_text_does_not_name_a_source():
    r = asyncio.run(arun_agent("q", llm=FakeLLM(agent=[text_reply()], synthesizer=[text_reply()]),
                               mcp=McpToolClient(HERE / "does_not_exist.py", startup_timeout=10)))
    assert r["status"] == "error" and "could not start" in r["answer"] and "Jira" not in r["answer"].split("(")[0]
