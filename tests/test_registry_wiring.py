"""D's registry wired into arun_agent, plus the full pipeline over the REAL Jira, Notion and email servers.

The model is scripted (no Ollama); everything else is real: three stdio server subprocesses, the full synthetic
data, the router, the scoped loops, the verifier. Record IDs are read from the data files, not hard-coded.
"""
import asyncio
import functools
import json

import pytest

from app import config
from app import sources as registry
from app.agent import arun_agent
from app.nodes.llm import LLMReply
from app.nodes.mcp_client import McpRouter
from tests.fakes import FakeLLM, synth_reply, text_reply, tool_reply
from tests.phase2_fakes import SOURCES, FakeMcp, SpyLLM, check_reply, plan_reply


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))


def first_id(path, key=None) -> str:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return (raw[key] if isinstance(raw, dict) else raw)[0]["id"]


def real_ids() -> tuple[str, str, str]:
    return (first_id(registry.JIRA.data_path, "tickets"), first_id(registry.NOTION.data_path),
            first_id(registry.EMAIL.data_path))


def one_each(ticket, page, email):
    """Agent script: each sub-question reads one known record with its own source's tool, then says DONE."""
    return [tool_reply("get_ticket", ticket_id=ticket), text_reply(),
            tool_reply("get_page", page_id=page), text_reply(),
            tool_reply("get_email", email_id=email), text_reply()]


# ---------------------------------------------------------------- wiring

@sync
async def test_planner_on_takes_the_client_and_sources_from_the_registry_and_enables_all_three(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    ticket, page, email = real_ids()
    fake = FakeMcp(results={"get_ticket": {"id": ticket}, "get_page": {"id": page}, "get_email": {"id": email}})
    monkeypatch.setattr("app.nodes.mcp_client.McpRouter", lambda *a, **k: fake)  # the router object only
    llm = SpyLLM(planner=[plan_reply(("jira", "a"), ("notion", "b"), ("email", "c"))], agent=one_each(ticket, page, email),
                 evidence_check=[check_reply(True)],
                 synthesizer=[synth_reply(f"[{ticket}] [{page}] [{email}]", [ticket, page, email])])
    r = await arun_agent("Q?", llm=llm)  # nothing injected: client, sources and known IDs all come from the registry
    assert [s.name for s in registry.all_sources()] == ["jira", "notion", "email"]
    assert r["status"] == "ok" and r["citations"] == [ticket, page, email]  # NTN and EML IDs are known IDs now
    assert "unverified" not in r["answer"] and r["sources"] == ["jira", "notion", "email"]
    prompt = llm.calls["planner"][0][1]["content"]  # the planner is shown each source and D's one-line description
    assert all(f"- {s.name}: {s.description}" in prompt for s in registry.all_sources())


@sync
async def test_the_registry_is_enabled_before_the_known_ids_are_read(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    order: list[str] = []
    real_runtime = registry.phase2_runtime
    monkeypatch.setattr("app.agent.phase2_runtime", lambda: (order.append("runtime"), real_runtime())[1])
    monkeypatch.setattr("app.agent.load_known_ids", lambda: (order.append("known_ids"), {"ATL-001"})[1])
    monkeypatch.setattr("app.nodes.mcp_client.McpRouter", lambda *a, **k: FakeMcp())
    await arun_agent("Q?", llm=FakeLLM(planner=[LLMReply(content="x")], agent=[text_reply()],
                                       evidence_check=[check_reply(True)], synthesizer=[synth_reply("x", [])]))
    assert order == ["runtime", "known_ids"]


@sync
async def test_planner_off_uses_the_phase1_client_and_leaves_the_registry_alone(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    used = []
    monkeypatch.setattr("app.agent.McpToolClient", lambda *a, **k: used.append("jira-client") or FakeMcp())
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="a"), text_reply()],
                  synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])
    r = await arun_agent("Q?", llm=llm, known_ids={"ATL-001"})
    assert used == ["jira-client"] and [s.name for s in registry.all_sources()] == ["jira"]
    assert set(r) == {"answer", "citations", "status", "trace_id", "tool_calls", "latency_ms"}


@sync
async def test_when_the_caller_injects_everything_the_registry_is_not_touched(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    monkeypatch.setattr("app.agent.phase2_runtime", lambda: pytest.fail("phase2_runtime should not be called"))
    llm = FakeLLM(planner=[plan_reply(("jira", "q"))], agent=[tool_reply("search_tickets", keyword="a"), text_reply()],
                  evidence_check=[check_reply(True)], synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])
    r = await arun_agent("Q?", llm=llm, mcp=FakeMcp(), known_ids={"ATL-001"}, sources=SOURCES)
    assert r["citations"] == ["ATL-001"]


@sync
async def test_a_source_with_an_unreadable_data_file_is_checked_against_the_evidence_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    monkeypatch.setenv("NOTION_DATA_PATH", str(tmp_path / "missing.json"))  # the verifier cannot list real page IDs
    ticket, _, _ = real_ids()
    monkeypatch.setattr("app.nodes.mcp_client.McpRouter", lambda *a, **k: FakeMcp(
        results={"get_ticket": {"id": ticket}, "get_page": {"id": "NTN-424"}}))
    llm = FakeLLM(planner=[plan_reply(("jira", "a"), ("notion", "b"))],
                  agent=[tool_reply("get_ticket", ticket_id=ticket), text_reply(),
                         tool_reply("get_page", page_id="NTN-424"), text_reply()],
                  evidence_check=[check_reply(True)],
                  synthesizer=[synth_reply(f"[{ticket}] and [NTN-424] and [NTN-999]", [ticket, "NTN-424", "NTN-999"])])
    r = await arun_agent("Q?", llm=llm)
    assert r["citations"] == [ticket, "NTN-424"]  # in the evidence: kept even though no list can vouch for it
    assert "[unverified: NTN-999]" in r["answer"]  # not in the evidence: still flagged


# ---------------------------------------------------------------- the REAL servers

@sync
async def test_full_pipeline_on_all_three_real_servers(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    ticket, page, email = real_ids()
    llm = SpyLLM(
        planner=[plan_reply(("jira", "look up a work item"), ("notion", "look up a page"), ("email", "look up a message"))],
        agent=one_each(ticket, page, email), evidence_check=[check_reply(True)],
        synthesizer=[synth_reply(f"Ticket [{ticket}], page [{page}], mail [{email}].", [ticket, page, email])],
    )
    r = await arun_agent("What do the tracker, the wiki and the mailbox say?", llm=llm)  # real router, real data
    assert r["status"] == "ok" and r["citations"] == [ticket, page, email] and "unverified" not in r["answer"]
    assert r["sources"] == ["jira", "notion", "email"]
    assert [c["name"] for c in r["tool_calls"]] == ["get_ticket", "get_page", "get_email"]
    assert [(p["source"], p["status"], p["steps"]) for p in r["plan"]] == [("jira", "resolved", 1), ("notion", "resolved", 1),
                                                                           ("email", "resolved", 1)]
    seen = llm.tools_seen["agent"]  # each loop saw only its own server's tools
    assert seen[0] == ["search_tickets", "get_ticket", "list_overdue"] and seen[2] == ["search_pages", "get_page"]
    assert seen[4] == ["search_emails", "get_email", "get_thread"]


@sync
async def test_a_real_server_that_cannot_read_its_data_is_reported_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    ticket, _, _ = real_ids()
    registry.enable_phase2_sources()  # first: the router takes its list of servers when it is built
    router = McpRouter(env={"notion": {"NOTION_DATA_PATH": str(tmp_path / "missing.json")}})
    llm = FakeLLM(planner=[plan_reply(("notion", "look up a page"), ("jira", "look up a work item"))],
                  agent=[tool_reply("get_page", page_id="NTN-001"), text_reply(),
                         tool_reply("get_ticket", ticket_id=ticket), text_reply()],
                  evidence_check=[check_reply(True)], synthesizer=[synth_reply(f"Ticket [{ticket}].", [ticket])])
    r = await arun_agent("Q?", llm=llm, mcp=router, sources=registry.scope_sources())
    assert [(p["source"], p["status"], p["outcome"]) for p in r["plan"]] == [
        ("notion", "unavailable", "data_unavailable"), ("jira", "resolved", "model")]
    assert "rewrite" not in llm.calls  # no point rewriting for a dead source
    assert r["answer"] == f"Ticket [{ticket}]. Source unavailable: notion." and r["citations"] == [ticket]


@sync
async def test_a_real_server_that_fails_to_start_is_reported_unavailable_without_a_model_call(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    ticket, _, _ = real_ids()
    registry.enable_phase2_sources()
    broken = [s if s.name != "email" else type(s)(**{**s.__dict__, "server_module": "app.mcp_servers.no_such_server"})
              for s in registry.all_sources()]
    router = McpRouter(sources=broken, startup_timeout=15)
    llm = FakeLLM(planner=[plan_reply(("email", "look up a message"), ("jira", "look up a work item"))],
                  agent=[tool_reply("get_ticket", ticket_id=ticket), text_reply()],
                  evidence_check=[check_reply(True)], synthesizer=[synth_reply(f"Ticket [{ticket}].", [ticket])])
    r = await arun_agent("Q?", llm=llm, mcp=router, sources=registry.scope_sources())
    assert [p["status"] for p in r["plan"]] == ["unavailable", "resolved"] and r["plan"][0]["unavailable"] == ["email"]
    assert llm.count("agent") == 2  # only the jira loop ran; the dead source cost no model call
    assert r["answer"].endswith("Source unavailable: email.") and r["citations"] == [ticket]
