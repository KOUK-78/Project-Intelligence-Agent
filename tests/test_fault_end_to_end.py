"""FAULT_INJECT through the whole agent: real servers, real router, scripted model. Nothing is stubbed but the LLM."""
import asyncio
import functools

import pytest

from app import config, sources
from app.agent import arun_agent
from app.nodes.mcp_client import McpRouter
from app.nodes.verify import load_known_ids
from tests.fakes import synth_reply, text_reply, tool_reply
from tests.phase2_fakes import SpyLLM, check_reply, plan_reply
from tests.test_source_modes import ENV


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    for env in ENV.values():
        for k, v in env.items():
            monkeypatch.setenv(k, v)


@sync
async def test_flat_run_with_notion_failing_still_answers_from_jira(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    monkeypatch.setenv("ENABLED_SOURCES", "all")
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    sources.phase2_runtime()
    router = McpRouter(env=ENV, startup_timeout=20)
    llm = SpyLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
                 synthesizer=[synth_reply("Blocked, see [ATL-112].", ["ATL-112"])])
    result = await arun_agent("q", llm=llm, mcp=router, known_ids=load_known_ids())
    assert result["status"] == "ok", result["answer"]
    assert result["citations"] == ["ATL-112"]
    offered = set(llm.tools_seen["agent"][0])
    assert offered == set(sources.JIRA.tool_names) | set(sources.EMAIL.tool_names)   # no Notion tool is offered


@sync
async def test_planner_run_with_notion_failing_reports_it_unavailable(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    client, planner_sources = sources.phase2_runtime()
    router = McpRouter(env=ENV, startup_timeout=20)
    llm = SpyLLM(
        planner=[plan_reply(("jira", "read the ticket"), ("notion", "read the page"))],
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-112]; the page is unavailable.", ["ATL-112"])],
    )
    result = await arun_agent("q", llm=llm, mcp=router, known_ids=load_known_ids(), sources=planner_sources)
    assert result["status"] == "ok", result["answer"]
    assert result["sources"] == ["jira"]                       # Notion returned nothing
    assert "Source unavailable: notion" in result["answer"]    # and the answer says so
    assert "NTN-104" not in result["citations"]
