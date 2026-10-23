"""D2 + B: the planned graph over the REAL router and three real stdio servers (stub data, scripted LLM)."""
import asyncio
import dataclasses
import functools
from pathlib import Path

import pytest

from app import config, sources
from app.agent import arun_agent
from app.nodes.mcp_client import McpRouter
from app.nodes.verify import load_known_ids
from tests.fakes import synth_reply, text_reply, tool_reply
from tests.phase2_fakes import SpyLLM, check_reply, plan_reply

FIX = Path(__file__).parent / "fixtures"
SYN = Path(__file__).parents[1] / "data" / "synthetic"
ENV = {"jira": {"JIRA_DATA_PATH": str(FIX / "jira_stub.json")},
       "notion": {"NOTION_DATA_PATH": str(SYN / "notion_pages_stub.json")},
       "email": {"EMAIL_DATA_PATH": str(SYN / "email_messages_stub.json")}}


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


@pytest.fixture(autouse=True)
def three_sources(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))
    sources.enable_phase2_sources()
    for env in ENV.values():
        for k, v in env.items():
            monkeypatch.setenv(k, v)


@sync
async def test_three_sources_through_the_router_give_a_verified_cited_answer():
    llm = SpyLLM(
        planner=[plan_reply(("jira", "read the ticket"), ("notion", "read the page"), ("email", "read the reply"))],
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply(),
               tool_reply("get_page", page_id="NTN-104"), text_reply(),
               tool_reply("get_email", email_id="EML-111"), text_reply()],
        evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-112], plan [NTN-104], vendor reply [EML-111].", ["ATL-112", "NTN-104", "EML-111"])],
    )
    router = McpRouter(env=ENV, startup_timeout=20)
    result = await arun_agent("q", llm=llm, mcp=router, known_ids=load_known_ids(), sources=sources.scope_sources())
    assert result["status"] == "ok", result["answer"]
    assert result["citations"] == ["ATL-112", "NTN-104", "EML-111"]
    assert result["sources"] == ["jira", "notion", "email"]
    assert llm.tools_seen["agent"][0] == ["search_tickets", "get_ticket", "list_overdue"]  # scoped per sub-question


@sync
async def test_a_dead_source_is_reported_and_the_others_still_answer():
    dead_email = dataclasses.replace(sources.EMAIL, server_module="app.mcp_servers.does_not_exist")
    router = McpRouter([sources.JIRA, sources.NOTION, dead_email], env=ENV, startup_timeout=20)
    llm = SpyLLM(
        planner=[plan_reply(("jira", "read the ticket"), ("email", "read the reply"))],
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-112]. The email source was unavailable.", ["ATL-112"])],
    )
    result = await arun_agent("q", llm=llm, mcp=router, known_ids=load_known_ids(), sources=sources.scope_sources())
    assert result["status"] != "error" or "unexpected" not in result["answer"]
    assert result["citations"] == ["ATL-112"]
    assert "email" in router.unavailable
    assert not any(t["name"] in sources.EMAIL.tool_names for t in result["tool_calls"])
