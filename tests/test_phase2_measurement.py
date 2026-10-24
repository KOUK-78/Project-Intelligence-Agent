"""C2/C5 against the REAL planned graph (B's) with B's fakes: what the runner reads and what the spans carry.

These tests exist because the runner and tracing were written against the contract, and the contract is only
true if the agent really produces it. No Ollama, no network.
"""
import asyncio

import pytest

from app import config, tracing
from app.agent import arun_agent
from evals import run_evals as ev
from tests.fakes import synth_reply, text_reply, tool_reply
from tests.phase2_fakes import SOURCES, FakeMcp, SpyLLM, check_reply, plan_reply
from tests.test_tracing import FakeTracer

IDS = {"ATL-001", "NTN-001", "EML-001"}
THREE = [tool_reply("search_tickets", keyword="a"), text_reply(), tool_reply("search_pages", keyword="b"), text_reply(),
         tool_reply("search_emails", keyword="c"), text_reply()]


def agent(**kw):
    return SpyLLM(usage={"planner": (100, 20), "agent": (50, 5), "evidence_check": (30, 2), "synthesizer": (80, 40)},
                  planner=[plan_reply(("jira", "find the ticket"), ("notion", "find the page"), ("email", "find the mail"))],
                  agent=THREE, evidence_check=[check_reply(True)],
                  synthesizer=[synth_reply("Ticket [ATL-001], page [NTN-001], mail [EML-001].", ["ATL-001", "NTN-001", "EML-001"])],
                  **kw)


@pytest.fixture(autouse=True)
def planner_on(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)


def run_traced(monkeypatch, tracer):
    monkeypatch.setattr(tracing, "_tracer", tracer)
    return asyncio.run(arun_agent("What do the tracker, wiki and mail say?", llm=agent(), mcp=FakeMcp(),
                                  known_ids=IDS, sources=SOURCES))


def test_the_runner_reads_a_real_planned_result():
    r = asyncio.run(arun_agent("q?", llm=agent(), mcp=FakeMcp(), known_ids=IDS, sources=SOURCES))
    f = ev.phase2_fields(r)
    assert (f["planner_used"], f["planner_valid"], f["planner_fallback"]) == (True, True, False)
    assert (f["subquestion_count"], f["subquestions_unresolved"], f["steps_total"]) == (3, 0, 3)
    assert f["sources_returning_records"] == ["jira", "notion", "email"] and f["sources_unavailable"] == []
    # planner 1 call, agent 6 (a tool round and a DONE round per sub-question), evidence check 3, synthesizer 1
    assert f["total_tokens"] == (100 + 20) + 6 * (50 + 5) + 3 * (30 + 2) + (80 + 40) == 666
    assert f["total_tokens"] == f["prompt_tokens"] + f["completion_tokens"]
    chk = ev.mechanical_checks(["ATL-001", "NTN-001"], r, IDS, [], ["jira", "notion", "email"], [])
    assert chk["all_sources_covered"] and chk["sources_cited"] == ["jira", "notion", "email"]


def test_stage_spans_carry_subquestion_and_source_attributes(monkeypatch):
    t = FakeTracer()
    run_traced(monkeypatch, t)
    by_name = {}
    for sp in t.spans:
        by_name.setdefault(sp.name, []).append(sp)
    planner = by_name["node:planner"][0].attrs
    assert planner["stage.subquestion_count"] == 3 and planner["stage.sources"] == "jira,notion,email"
    assert planner["stage.planner_fallback"] is False
    checks = by_name["node:evidence_check"]
    assert [c.attrs["stage.source"] for c in checks] == ["jira", "notion", "email"]
    assert [c.attrs["stage.subquestion_index"] for c in checks] == [0, 1, 2]
    assert [c.attrs["stage.subquestion_id"] for c in checks] == ["sq1", "sq2", "sq3"]
    assert all(c.attrs["stage.outcome"] == "resolved" and c.attrs["stage.subquestion"] for c in checks)
    retrieves = by_name["node:retrieve"]
    assert [r.attrs["stage.source"] for r in retrieves] == ["jira", "notion", "email"]
    assert all(r.attrs["stage.evidence_items"] >= 1 for r in retrieves)


def test_every_planned_graph_node_gets_a_span_in_one_trace():
    """The fake LLM bypasses OllamaLLM, so the model-call span is covered in tests/test_tracing.py; here we only
    prove the node spans for the planned graph all exist."""
    t = FakeTracer()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(config, "PLANNER_ENABLED", True)
        run_traced(mp, t)
    finally:
        mp.undo()
    names = [s.name for s in t.spans]
    for expected in ("node:planner", "node:retrieve", "node:evidence_check", "node:synthesizer"):
        assert expected in names, expected


def test_attributes_are_read_safely_from_odd_state(monkeypatch):
    for odd in (None, {}, {"plan": "x", "sq_index": "y"}, {"plan": [{"id": "a"}], "sq_index": 5}, [1, 2]):
        assert tracing._stage_attrs("evidence_check", odd).get("stage.source") is None
    assert tracing._stage_attrs("agent", {"plan": [{"id": "a", "source": "s"}], "sq_index": 0}) == {}
    assert tracing._result_attrs("evidence_check", None) == {} and tracing._result_attrs("agent", {"route": "x"}) == {}


def test_phase1_graph_spans_are_unchanged_with_the_planner_off(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    t = FakeTracer()
    monkeypatch.setattr(tracing, "_tracer", t)
    from tests.test_agent import STUB_IDS, jira
    from tests.fakes import FakeLLM
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="payments"), text_reply()],
                  synthesizer=[synth_reply("x [ATL-112].", ["ATL-112"])])
    asyncio.run(arun_agent("What is blocking?", llm=llm, mcp=jira(), known_ids=STUB_IDS))
    names = {s.name for s in t.spans}
    assert {"node:agent", "node:tool_executor", "node:synthesizer"} <= names
    assert not names & {"node:planner", "node:retrieve", "node:evidence_check"}
    assert not any(k.startswith("stage.") for s in t.spans for k in s.attrs)


# ---------------------------------------------------------------- the people check ignores mailboxes, not people
def test_team_mailboxes_are_not_people_but_real_names_still_are(monkeypatch, tmp_path):
    import json

    from tests import corpus
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps([{"id": "EML-901", "subject": "a long enough subject",
                                 "from": "People Team <people@x.example>", "to": ["Priya Raman <priya@x.example>", "Atlas Team <atlas-team@x.example>"]}]))
    monkeypatch.setattr(config, "DATA_PATHS", {"jira": tmp_path / "none.json", "notion": tmp_path / "none2.json", "email": mail})
    f = corpus.facts("email")
    assert "people" not in f["names"] and "atlas" not in f["names"]           # mailboxes: ordinary words
    assert {"priya raman", "priya", "raman"} <= f["names"]                    # a person stays checked
    assert {"people@x.example", "priya@x.example"} <= f["addresses"]          # addresses stay checked either way
