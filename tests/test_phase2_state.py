"""B1: Phase 2 state, evidence tagging, per-sub-question reset, and the scoped tool view."""
import asyncio

from app import config
from app.nodes.agent_node import step_cap
from app.nodes.scope import ALL_SOURCES, ScopedClient, scope_tool_names, tool_source_map, unavailable_sources
from app.nodes.state import evidence_for, fresh_subquestion_state, tag_evidence
from tests.phase2_fakes import ALL_TOOLS, SOURCES, FakeMcp, spec


def test_step_cap_defaults_to_the_phase1_config_value():
    assert step_cap({}) == config.MAX_AGENT_STEPS
    assert step_cap({"step_cap": 2}) == 2


def test_fresh_subquestion_state_resets_every_phase1_flag():
    s = fresh_subquestion_state("q", 3)
    assert s["question"] == "q" and s["messages"] == [{"role": "user", "content": "q"}]
    assert s["steps"] == 0 and s["consecutive_errors"] == 0 and s["step_cap"] == 3
    for flag in ("nudged", "detail_nudged", "ref_nudged", "auto_round", "capped"):
        assert s[flag] is False, flag
    assert s["evidence"] == [] and s["tool_calls"] == [] and s["status"] == "" and s["route"] == ""


def test_each_call_returns_independent_lists():
    a, b = fresh_subquestion_state("q", 1), fresh_subquestion_state("q", 1)
    a["evidence"].append({"x": 1})
    a["messages"].append({"role": "assistant", "content": ""})
    assert b["evidence"] == [] and len(b["messages"]) == 1


def test_tagging_keeps_phase1_keys_and_does_not_mutate_the_input():
    items = [{"tool": "get_page", "args": {}, "ok": True, "data": {"id": "NTN-001"}, "error": None}]
    tagged = tag_evidence(items, "sq2", "all", tool_source_map(SOURCES))
    assert tagged[0]["sq"] == "sq2" and tagged[0]["source"] == "notion"  # the tool decides, not the "all" scope
    assert tagged[0]["tool"] == "get_page" and tagged[0]["ok"] is True
    assert "sq" not in items[0]


def test_unknown_tool_falls_back_to_the_sub_question_source():
    assert tag_evidence([{"tool": "mystery"}], "sq1", "email", tool_source_map(SOURCES))[0]["source"] == "email"


def test_evidence_for_filters_by_sub_question():
    ev = [{"sq": "sq1", "n": 1}, {"sq": "sq2", "n": 2}, {"sq": "sq1", "n": 3}]
    assert [e["n"] for e in evidence_for(ev, "sq1")] == [1, 3]


def test_scope_lists_only_that_sources_tools():
    names = scope_tool_names(SOURCES, "notion", ALL_TOOLS)
    assert names == ["search_pages", "get_page"]
    assert scope_tool_names(SOURCES, "nope", ALL_TOOLS) == []


def test_all_scope_skips_tools_of_sources_that_are_down():
    down = [s if s.name != "email" else type(s)(s.name, s.description, s.tools, available=False) for s in SOURCES]
    assert unavailable_sources(down, ALL_TOOLS) == {"email"}
    names = scope_tool_names(down, ALL_SOURCES, ALL_TOOLS)
    assert "search_emails" not in names and "search_tickets" in names and "get_page" in names


def test_a_source_whose_tools_are_not_advertised_counts_as_unavailable():
    live = [spec(t) for t in ("search_tickets", "get_ticket", "list_overdue")]
    assert unavailable_sources(SOURCES, live) == {"notion", "email"}


def test_scoped_client_hides_and_refuses_other_tools():
    mcp = FakeMcp()
    scoped = ScopedClient(mcp, scope_tool_names(SOURCES, "jira", mcp.tools))
    assert [t["name"] for t in scoped.tools] == ["search_tickets", "get_ticket", "list_overdue"]
    refused = asyncio.run(scoped.call("search_pages", {"keyword": "x"}))
    assert not refused.ok and refused.error.startswith("unknown_tool") and mcp.calls == []
    allowed = asyncio.run(scoped.call("get_ticket", {"ticket_id": "ATL-001"}))
    assert allowed.ok and mcp.calls == [("get_ticket", {"ticket_id": "ATL-001"})]
