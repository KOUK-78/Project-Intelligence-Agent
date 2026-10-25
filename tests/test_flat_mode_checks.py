"""Flat mode (planner off, several sources behind one router) must check citations like the planned graph does.

D's review: `unchecked` used to be computed only with the planner on, so with ENABLED_SOURCES=all and the planner off
an unreadable source's IDs were flagged as unknown. A Jira-only run (Phase 1) must stay exactly as it was.
"""
import asyncio
import functools

import pytest

from app import config
from app import sources as registry
from app.agent import arun_agent
from app.nodes.synthesizer import make_synthesizer_node
from tests.fakes import FakeLLM, synth_reply, text_reply, tool_reply
from tests.phase2_fakes import ALL_TOOLS, FakeMcp, SpyLLM

PHASE1_KEYS = {"answer", "citations", "status", "trace_id", "tool_calls", "latency_ms"}


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)


def evidence(*ids):
    return [{"tool": "get_page", "args": {}, "ok": True, "data": {"id": i}, "error": None} for i in ids]


def run_node(llm, known, unchecked=None, ev=None):
    node = make_synthesizer_node(llm, known) if unchecked is None else make_synthesizer_node(llm, known, unchecked)
    return asyncio.run(node({"question": "Q?", "evidence": ev if ev is not None else evidence("ATL-001", "NTN-001")}))


# ---------------------------------------------------------------- the Phase 1 synthesizer node

def test_the_phase1_node_checks_an_unreadable_sources_ids_against_the_evidence_only():
    reply = synth_reply("Tracker [ATL-001], wiki [NTN-001].", ["ATL-001", "NTN-001"])
    strict = run_node(FakeLLM(synthesizer=[reply]), {"ATL-001"})
    assert strict["citations"] == ["ATL-001"] and "[unverified: NTN-001]" in strict["answer"]  # the old behaviour
    lenient = run_node(FakeLLM(synthesizer=[reply]), {"ATL-001"}, {"NTN"})
    assert lenient["citations"] == ["ATL-001", "NTN-001"] and "unverified" not in lenient["answer"]


def test_an_unchecked_prefix_still_does_not_excuse_an_id_missing_from_the_evidence():
    llm = FakeLLM(synthesizer=[synth_reply("Tracker [ATL-001] and [NTN-999].", ["ATL-001", "NTN-999"])])
    out = run_node(llm, {"ATL-001"}, {"NTN"})
    assert out["citations"] == ["ATL-001"] and "[unverified: NTN-999]" in out["answer"]
    assert "not_in_evidence:NTN-999" in out["warnings"]


def test_the_default_is_no_unchecked_prefixes_so_phase1_callers_are_unaffected():
    reply = synth_reply("Wiki [NTN-001].", ["NTN-001"])
    default = run_node(FakeLLM(synthesizer=[reply]), {"ATL-001"}, None, evidence("NTN-001"))
    assert "[unverified: NTN-001]" in default["answer"]


# ---------------------------------------------------------------- arun_agent in flat mode

@sync
async def test_flat_mode_does_not_flag_ids_of_a_source_whose_data_file_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLED_SOURCES", "all")  # planner off + several sources = flat mode
    monkeypatch.setenv("NOTION_DATA_PATH", str(tmp_path / "missing.json"))  # no list of real page IDs exists
    monkeypatch.setattr("app.nodes.mcp_client.McpRouter",
                        lambda *a, **k: FakeMcp(results={"get_page": {"id": "NTN-424"}}))
    llm = SpyLLM(agent=[tool_reply("get_page", page_id="NTN-424"), text_reply()],
                 synthesizer=[synth_reply("Page [NTN-424] and [NTN-999].", ["NTN-424", "NTN-999"])])
    r = await arun_agent("Q?", llm=llm)
    assert llm.tools_seen["agent"][0] == [t["name"] for t in ALL_TOOLS]  # flat: one loop over every tool
    assert r["citations"] == ["NTN-424"]  # in the evidence: kept even though no list can vouch for it
    assert "[unverified: NTN-999]" in r["answer"] and "[unverified: NTN-424]" not in r["answer"]
    assert "plan" not in r  # flat mode is the planner-off shape


@sync
async def test_flat_mode_still_flags_unknown_ids_of_sources_whose_data_is_readable(monkeypatch):
    monkeypatch.setenv("ENABLED_SOURCES", "all")
    monkeypatch.setattr("app.nodes.mcp_client.McpRouter",
                        lambda *a, **k: FakeMcp(results={"get_page": {"id": "NTN-9999"}}))  # not in the real data
    llm = FakeLLM(agent=[tool_reply("get_page", page_id="NTN-9999"), text_reply()],
                  synthesizer=[synth_reply("Page [NTN-9999].", ["NTN-9999"])])
    r = await arun_agent("Q?", llm=llm)
    assert r["citations"] == [] and "[unverified: NTN-9999]" in r["answer"]  # readable data: the check still bites


@sync
async def test_a_jira_only_phase1_run_is_unchanged_even_when_its_data_file_is_unreadable(monkeypatch, tmp_path):
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    monkeypatch.setenv("JIRA_DATA_PATH", str(tmp_path / "missing.json"))  # no ID list at all: evidence-only, as before
    monkeypatch.setattr("app.agent.McpToolClient", lambda *a, **k: FakeMcp())
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="a"), text_reply()],
                  synthesizer=[synth_reply("T [ATL-001] and [ATL-777].", ["ATL-001", "ATL-777"])])
    r = await arun_agent("Q?", llm=llm)
    assert set(r) == PHASE1_KEYS
    assert r["citations"] == ["ATL-001"] and "[unverified: ATL-777]" in r["answer"]  # not in evidence: still flagged
    assert [s.name for s in registry.all_sources()] == ["jira"]
