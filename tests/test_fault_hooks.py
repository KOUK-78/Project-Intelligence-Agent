"""app/faults.py wired into D's per-source paths: inject(source, "startup") before a server is launched and
inject(source, "call") inside the call timeout. Real router, real stdio servers."""
import asyncio
import functools
import time
from pathlib import Path

import pytest

from app import faults, sources
from app.nodes.mcp_client import McpRouter, McpStartupError, McpToolClient

FIX = Path(__file__).parent / "fixtures"
SYN = Path(__file__).parents[1] / "data" / "synthetic"
ENV = {"jira": {"JIRA_DATA_PATH": str(FIX / "jira_stub.json")},
       "notion": {"NOTION_DATA_PATH": str(SYN / "notion_pages_stub.json")},
       "email": {"EMAIL_DATA_PATH": str(SYN / "email_messages_stub.json")}}
THREE = [sources.JIRA, sources.NOTION, sources.EMAIL]


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("FAULT_INJECT", raising=False)


@sync
async def test_no_fault_means_everything_starts_and_answers():
    async with McpRouter(THREE, env=ENV, startup_timeout=20) as r:
        assert r.unavailable == {} and (await r.call("get_page", {"page_id": "NTN-104"})).ok


@sync
async def test_notion_fail_makes_notion_unavailable_in_a_real_router_run(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    async with McpRouter(THREE, env=ENV, startup_timeout=20) as r:
        assert r.started == ["jira", "email"] or set(r.started) == {"jira", "email"}
        assert "injected fault: notion startup failed" in r.unavailable["notion"]
        out = await r.call("get_page", {"page_id": "NTN-104"})
        assert not out.ok and out.error.startswith("source_unavailable: notion")
        assert (await r.call("get_ticket", {"ticket_id": "ATL-112"})).ok   # the other sources carry on
        assert (await r.call("get_email", {"email_id": "EML-111"})).ok


@sync
async def test_injected_startup_failure_never_launches_the_server(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    # a module that cannot exist: if the server were launched the error would mention that module instead
    client = McpToolClient(server_module="app.mcp_servers.does_not_exist", source="notion")
    with pytest.raises(McpStartupError, match="injected fault: notion startup failed"):
        async with client:
            pass


@sync
async def test_fault_for_one_source_does_not_touch_the_others(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "email:fail")
    async with McpRouter(THREE, env=ENV, startup_timeout=20) as r:
        assert set(r.unavailable) == {"email"}
        assert (await r.call("get_page", {"page_id": "NTN-104"})).ok


@sync
async def test_call_stage_fail_after_a_healthy_start_is_a_call_failed_outcome(monkeypatch):
    async with McpRouter(THREE, env=ENV, startup_timeout=20) as r:
        assert r.unavailable == {}
        monkeypatch.setenv("FAULT_INJECT", "notion:fail")        # read on every call, so it can change mid-run
        out = await r.call("get_page", {"page_id": "NTN-104"})
        assert not out.ok and out.error.startswith("call_failed:") and "injected fault" in out.error
        assert (await r.call("get_ticket", {"ticket_id": "ATL-112"})).ok


@sync
async def test_timeout_fault_trips_the_real_call_timeout(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "email:timeout")
    async with McpRouter(THREE, env=ENV, startup_timeout=20, call_timeout=0.4) as r:
        assert r.unavailable == {}                               # startup is allowed to succeed
        t = time.perf_counter()
        out = await r.call("get_email", {"email_id": "EML-111"})
        assert not out.ok and out.error.startswith("timeout:")
        assert time.perf_counter() - t < 5                       # the 0.4 s timeout fired, not the 30 s injected sleep
        assert (await r.call("get_page", {"page_id": "NTN-104"})).ok


@sync
async def test_fail_and_timeout_together(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "notion:fail,email:timeout")
    async with McpRouter(THREE, env=ENV, startup_timeout=20, call_timeout=0.4) as r:
        assert set(r.unavailable) == {"notion"}
        assert (await r.call("get_email", {"email_id": "EML-111"})).error.startswith("timeout:")
        assert (await r.call("get_ticket", {"ticket_id": "ATL-112"})).ok


@sync
async def test_every_source_failing_is_a_startup_error(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "jira:fail,notion:fail,email:fail")
    with pytest.raises(McpStartupError, match="no MCP server"):
        async with McpRouter(THREE, env=ENV, startup_timeout=20):
            pass


def test_a_bad_fault_spec_is_rejected_loudly(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "slack:fail")
    with pytest.raises(ValueError, match="FAULT_INJECT"):
        asyncio.run(faults.inject("jira", "call"))


@sync
async def test_a_single_server_client_without_a_source_name_ignores_faults(monkeypatch):
    # Phase 1 path: McpToolClient() is built without a source, so FAULT_INJECT cannot change it
    monkeypatch.setenv("FAULT_INJECT", "jira:fail")
    monkeypatch.setenv("JIRA_DATA_PATH", str(FIX / "jira_stub.json"))
    async with McpToolClient(env={"JIRA_DATA_PATH": str(FIX / "jira_stub.json")}) as c:
        assert (await c.call("get_ticket", {"ticket_id": "ATL-112"})).ok
