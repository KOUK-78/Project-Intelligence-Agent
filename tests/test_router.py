"""MCP router tests (D2). Fake clients for logic; one real subprocess for the failure path."""
import asyncio
import functools
import time
from pathlib import Path

import pytest

from app.nodes.mcp_client import McpRouter, McpStartupError, ToolOutcome
from app.sources import Source, get_source

STUB = Path(__file__).parent / "fixtures" / "jira_stub.json"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


class FakeClient:
    def __init__(self, tools, *, fail=None, delay=0.0):
        self.tools = [{"name": t, "description": "", "input_schema": {}} for t in tools]
        self.fail, self.delay, self.calls, self.entered, self.exited = fail, delay, [], False, False

    async def __aenter__(self):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise McpStartupError(self.fail)
        self.entered = True
        return self

    async def __aexit__(self, *a):
        self.exited = True

    async def call(self, name, args):
        self.calls.append(name)
        return ToolOutcome(True, {"tool": name})


def src(name, prefix, tools):
    return Source(name=name, server_module="m", id_prefix=prefix, data_env=f"{prefix}_DATA",
                  data_path=Path("."), tool_names=tuple(tools))


A, B, C = src("a", "AAA", ["a_one"]), src("b", "BBB", ["b_one", "b_two"]), src("c", "CCC", ["c_one"])


@sync
async def test_merges_tools_and_routes_by_name():
    fa, fb = FakeClient(["a_one"]), FakeClient(["b_one", "b_two"])
    async with McpRouter([A, B], clients={"a": fa, "b": fb}) as r:
        assert {t["name"] for t in r.tools} == {"a_one", "b_one", "b_two"}
        assert (await r.call("b_two", {})).data == {"tool": "b_two"}
        assert fa.calls == [] and fb.calls == ["b_two"]
        assert (await r.call("nope", {})).error.startswith("unknown_tool")
    assert fa.exited and fb.exited


@sync
async def test_servers_start_concurrently():
    clients = {n: FakeClient([f"{n}_one"], delay=0.4) for n in "abc"}
    t = time.perf_counter()
    async with McpRouter([A, B, C], clients={**clients, "b": FakeClient(["b_one"], delay=0.4)}):
        pass
    assert time.perf_counter() - t < 0.9  # three 0.4 s starts, not 1.2 s


@sync
async def test_failed_server_is_unavailable_and_run_continues():
    ok, bad = FakeClient(["a_one"]), FakeClient(["b_one"], fail="boom")
    async with McpRouter([A, B], clients={"a": ok, "b": bad}) as r:
        assert r.started == ["a"] and "boom" in r.unavailable["b"]
        out = await r.call("b_one", {})
        assert not out.ok and out.error.startswith("source_unavailable: b")
        assert (await r.call("a_one", {})).ok


@sync
async def test_all_servers_failing_is_a_startup_error():
    with pytest.raises(McpStartupError, match="no MCP server"):
        async with McpRouter([A, B], clients={"a": FakeClient(["a_one"], fail="x"), "b": FakeClient(["b_one"], fail="y")}):
            pass


@sync
async def test_duplicate_tool_names_rejected_at_startup():
    with pytest.raises(McpStartupError, match="duplicate tool name 'x'"):
        async with McpRouter([A, B], clients={"a": FakeClient(["x"]), "b": FakeClient(["x"])}):
            pass


@sync
async def test_lazy_starts_only_what_is_asked():
    fa, fb = FakeClient(["a_one"]), FakeClient(["b_one"])
    async with McpRouter([A, B], clients={"a": fa, "b": fb}, lazy=True) as r:
        assert r.tools == [] and not fa.entered
        await r.ensure(["b"])
        assert fb.entered and not fa.entered and r.started == ["b"]
        await r.ensure(["b"])  # idempotent
        assert (await r.call("b_one", {})).ok
    assert fb.exited


@sync
async def test_real_jira_server_through_router_with_a_dead_source():
    dead = Source(name="dead", server_module="app.mcp_servers.does_not_exist", id_prefix="DDD",
                  data_env="D_DATA", data_path=Path("."), tool_names=("dead_tool",))
    jira = get_source("jira")
    async with McpRouter([jira, dead], env={"jira": {"JIRA_DATA_PATH": str(STUB)}}, startup_timeout=15) as r:
        assert r.started == ["jira"] and "dead" in r.unavailable
        assert {t["name"] for t in r.tools} == set(jira.tool_names)
        out = await r.call("search_tickets", {"keyword": "*"})
        assert out.ok and "count" in out.data
        assert (await r.call("dead_tool", {})).error.startswith("source_unavailable")
