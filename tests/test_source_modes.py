"""The ENABLED_SOURCES switch: Phase 1 mode, flat mode (M2 baseline) and planner mode. Real servers, scripted LLM."""
import asyncio
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
ALL_TOOLS = {t for s in (sources.JIRA, sources.NOTION, sources.EMAIL) for t in s.tool_names}


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    monkeypatch.delenv("FAULT_INJECT", raising=False)
    for env in ENV.values():
        for k, v in env.items():
            monkeypatch.setenv(k, v)


# ------------------------------------------------------------------ the switch itself
@pytest.mark.parametrize("raw,planner,expected", [
    (None, False, ("jira",)),                              # Phase 1 default
    (None, True, ("jira", "notion", "email")),             # planner default
    ("all", False, ("jira", "notion", "email")),           # flat mode
    ("email, jira", False, ("jira", "email")),             # any subset, registry order
    (" ALL ", True, ("jira", "notion", "email")),
    ("notion", True, ("notion",)),
])
def test_resolve_enabled_sources(raw, planner, expected):
    env = {} if raw is None else {"ENABLED_SOURCES": raw}
    assert config.resolve_enabled_sources(env, planner) == expected


@pytest.mark.parametrize("raw", ["slack", "jira,slack", ",", "none"])
def test_resolve_enabled_sources_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="ENABLED_SOURCES"):
        config.resolve_enabled_sources({"ENABLED_SOURCES": raw}, False)


# ------------------------------------------------------------------ mode 1: Phase 1 regression (Jira only)
def test_mode_jira_only_is_phase1(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    assert sources.phase2_runtime() == (None, None)  # arun_agent then uses the default single-server client
    assert [s.name for s in sources.all_sources()] == ["jira"]


def test_jira_only_mode_resets_a_registry_that_was_widened(monkeypatch):
    sources.enable_phase2_sources()
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    sources.phase2_runtime()
    assert [s.name for s in sources.all_sources()] == ["jira"]


# ------------------------------------------------------------------ mode 2: flat (planner off, all sources)
def test_mode_flat_returns_a_router_and_no_planner_sources(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    monkeypatch.setenv("ENABLED_SOURCES", "all")
    client, planner_sources = sources.phase2_runtime()
    assert isinstance(client, McpRouter) and planner_sources is None
    assert [s.name for s in sources.all_sources()] == ["jira", "notion", "email"]


@sync
async def test_flat_mode_runs_one_flat_loop_over_all_tools(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    monkeypatch.setenv("ENABLED_SOURCES", "all")
    client, planner_sources = sources.phase2_runtime()
    client = McpRouter(env=ENV, startup_timeout=20)  # same router, pointed at the stub data
    llm = SpyLLM(
        agent=[tool_reply("get_page", page_id="NTN-104"), text_reply()],
        synthesizer=[synth_reply("The plan is in [NTN-104].", ["NTN-104"])],
    )
    result = await arun_agent("q", llm=llm, mcp=client, known_ids=load_known_ids(), sources=planner_sources)
    assert result["status"] == "ok", result["answer"]
    assert result["citations"] == ["NTN-104"]
    assert set(llm.tools_seen["agent"][0]) == ALL_TOOLS      # ONE loop sees every tool, unscoped
    assert "planner" not in llm.tools_seen                     # no planner call in flat mode
    assert not result.get("plan")


# ------------------------------------------------------------------ mode 3: planner over all sources
def test_mode_planner_returns_router_and_scoped_sources(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    client, planner_sources = sources.phase2_runtime()
    assert isinstance(client, McpRouter)
    assert [s.name for s in planner_sources] == ["jira", "notion", "email"]


def test_planner_mode_respects_a_source_subset(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    monkeypatch.setenv("ENABLED_SOURCES", "jira,notion")
    _, planner_sources = sources.phase2_runtime()
    assert [s.name for s in planner_sources] == ["jira", "notion"]
    assert {s.name for s in sources.all_sources()} == {"jira", "notion"}


@sync
async def test_planner_mode_still_scopes_tools_per_subquestion(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    client, planner_sources = sources.phase2_runtime()
    client = McpRouter(env=ENV, startup_timeout=20)
    llm = SpyLLM(
        planner=[plan_reply(("jira", "read the ticket"), ("notion", "read the page"))],
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply(), tool_reply("get_page", page_id="NTN-104"), text_reply()],
        evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-112], plan [NTN-104].", ["ATL-112", "NTN-104"])],
    )
    result = await arun_agent("q", llm=llm, mcp=client, known_ids=load_known_ids(), sources=planner_sources)
    assert result["status"] == "ok", result["answer"]
    assert set(llm.tools_seen["agent"][0]) == set(sources.JIRA.tool_names)
    assert set(llm.tools_seen["agent"][2]) == set(sources.NOTION.tool_names)
