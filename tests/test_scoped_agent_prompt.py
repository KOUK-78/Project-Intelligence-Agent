"""The agent loop must describe the tools it actually has. Planner-on M2: an email-only scope was told it answers
questions about tickets and nudged to call get_ticket, which it did not have. Ticket scopes must stay unchanged."""
import asyncio

from app.nodes.agent_node import AGENT_SYSTEM, DETAIL_NUDGE, make_agent_node
from tests.fakes import FakeLLM, text_reply, tool_reply
from tests.phase2_fakes import ALL_TOOLS, spec

JIRA = [spec(n) for n in ("search_tickets", "get_ticket", "list_overdue")]
EMAIL = [spec(n) for n in ("search_emails", "get_email", "get_thread")]


def first_messages(tools, question="Q?", evidence=None, replies=None):
    llm = FakeLLM(agent=replies or [text_reply()])
    state = {"question": question, "messages": [{"role": "user", "content": question}], "evidence": evidence or [],
             "steps": 0, "step_cap": 5}
    asyncio.run(make_agent_node(llm, tools)(state))
    return llm.calls["agent"][0]


def test_a_ticket_only_scope_keeps_the_phase1_prompt_exactly():
    assert first_messages(JIRA)[0]["content"] == AGENT_SYSTEM


def test_a_scope_with_several_sources_gets_the_same_neutral_prompt_as_a_single_other_source():
    """Flat mode (every source, no planner) must not be handicapped by the ticket-only prompt (C's fairness rule)."""
    flat = first_messages(ALL_TOOLS)[0]["content"]
    assert flat != AGENT_SYSTEM and "ticket" not in flat.lower()
    assert flat == first_messages(EMAIL)[0]["content"]


def test_a_scope_without_ticket_tools_gets_a_prompt_that_does_not_talk_about_tickets():
    text = first_messages(EMAIL)[0]["content"]
    assert "ticket" not in text.lower() and "search_tickets" not in text and "get_" in text


def test_the_detail_nudge_names_a_tool_the_scope_has():
    ev = [{"tool": "search_emails", "args": {}, "ok": True, "data": {"results": [{"id": "EML-001"}]}, "error": None}]
    llm = FakeLLM(agent=[text_reply()])
    state = {"question": "Why is it late?", "messages": [{"role": "user", "content": "x"}], "evidence": ev,
             "nudged": True, "steps": 1, "step_cap": 5}
    out = asyncio.run(make_agent_node(llm, EMAIL)(state))
    nudge = out["messages"][-1]["content"]
    assert "get_email" in nudge and "get_ticket" not in nudge


def test_the_ticket_detail_nudge_is_unchanged():
    ev = [{"tool": "search_tickets", "args": {}, "ok": True, "data": {"results": [{"id": "ATL-001"}]}, "error": None}]
    state = {"question": "Why is it late?", "messages": [{"role": "user", "content": "x"}], "evidence": ev,
             "nudged": True, "steps": 1, "step_cap": 5}
    out = asyncio.run(make_agent_node(FakeLLM(agent=[text_reply()]), JIRA)(state))
    assert out["messages"][-1]["content"] == DETAIL_NUDGE


def test_planner_off_without_other_sources_still_uses_the_phase1_prompt_end_to_end(monkeypatch):
    """The Phase 1 reproduction gate: planner off + ENABLED_SOURCES unset reaches Jira only, with the Phase 1 prompt."""
    from app import config
    from app.agent import arun_agent
    from tests.phase2_fakes import FakeMcp
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    monkeypatch.setattr("app.agent.McpToolClient", lambda *a, **k: FakeMcp(tools=list(JIRA)))
    llm = FakeLLM(agent=[text_reply()], synthesizer=[text_reply("{}")])
    asyncio.run(arun_agent("Q?", llm=llm))
    assert llm.calls["agent"][0][0]["content"] == AGENT_SYSTEM
