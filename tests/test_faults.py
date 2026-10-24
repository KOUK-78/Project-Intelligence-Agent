"""C4: fault injection. The switch parses strictly, does nothing when unset, and makes a source fail or hang."""
import asyncio
import time
from pathlib import Path

import pytest

from app import config, faults
from app.agent import arun_agent
from app.nodes.mcp_client import McpStartupError, McpToolClient
from tests.fakes import FakeLLM, synth_reply, text_reply, tool_reply

STUB_DATA = Path(__file__).parent / "fixtures" / "jira_stub.json"
STUB_IDS = {"ATL-109", "ATL-112", "ATL-118"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("FAULT_INJECT", raising=False)


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("raw,expected", [
    ("", {}), ("  ", {}), ("notion:fail", {"notion": "fail"}),
    ("email:timeout", {"email": "timeout"}), (" Notion : FAIL , email:timeout ", {"notion": "fail", "email": "timeout"}),
    ("jira:fail,", {"jira": "fail"}), ("notion:fail,notion:timeout", {"notion": "timeout"}),   # last one wins
])
def test_fault_inject_parses(raw, expected):
    assert config.resolve_faults({"FAULT_INJECT": raw}) == expected


@pytest.mark.parametrize("raw", ["notion", "notion:", ":fail", "slack:fail", "notion:explode", "notion:fail:extra"])
def test_bad_entries_are_rejected_loudly(raw):
    with pytest.raises(ValueError, match="FAULT_INJECT"):
        config.resolve_faults({"FAULT_INJECT": raw})


def test_unset_means_no_faults_and_inject_is_a_no_op(monkeypatch):
    assert faults.active() == {} and faults.mode_for("notion") is None
    for source in config.SOURCES:
        for stage in faults.STAGES:
            run(faults.inject(source, stage))      # returns at once, raises nothing


def test_the_variable_is_read_on_every_call(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "email:fail")
    assert faults.mode_for("email") == "fail"
    monkeypatch.setenv("FAULT_INJECT", "")
    assert faults.mode_for("email") is None


def test_unknown_stage_is_an_error():
    with pytest.raises(ValueError, match="stage"):
        run(faults.inject("notion", "midway"))


# ---------------------------------------------------------------- inject()
def test_fail_raises_at_both_stages_for_the_chosen_source_only(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    for stage in faults.STAGES:
        with pytest.raises(faults.SourceFault) as e:
            run(faults.inject("notion", stage))
        assert (e.value.source, e.value.stage) == ("notion", stage)
    run(faults.inject("jira", "call"))
    run(faults.inject("email", "startup"))


def test_timeout_hangs_calls_but_lets_startup_succeed(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "email:timeout")
    run(faults.inject("email", "startup"))                       # the server is up; calls are what hang

    async def hung():
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(faults.inject("email", "call"), timeout=0.2)
        except asyncio.TimeoutError:
            return time.perf_counter() - t0
        raise AssertionError("an injected timeout must hang past the caller's timeout")

    assert run(hung()) < 1.0                                     # cancelled promptly, did not sleep the full time


def test_the_injected_hang_outlasts_the_real_call_timeout():
    assert config.FAULT_TIMEOUT_SLEEP_S > config.MCP_CALL_TIMEOUT_S


# ---------------------------------------------------------------- FaultyClient around the real Jira server
def jira_client():
    return McpToolClient(env={"JIRA_DATA_PATH": str(STUB_DATA)})


def test_wrapper_is_transparent_without_faults():
    async def go():
        async with faults.FaultyClient(jira_client(), "jira") as c:
            assert {t["name"] for t in c.tools} >= {"search_tickets", "get_ticket", "list_overdue"}
            out = await c.call("get_ticket", {"ticket_id": "ATL-112"})
            return out

    out = run(go())
    assert out.ok and out.data["id"] == "ATL-112"


def test_wrapper_fail_blocks_startup_before_the_server_launches(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "jira:fail")
    launched = []

    class Probe(McpToolClient):
        async def __aenter__(self):
            launched.append(True)
            return await super().__aenter__()

    async def go():
        async with faults.FaultyClient(Probe(env={"JIRA_DATA_PATH": str(STUB_DATA)}), "jira"):
            pass

    with pytest.raises(faults.SourceFault):
        run(go())
    assert launched == []


def test_wrapper_timeout_ends_as_a_normal_timeout_outcome(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "jira:timeout")

    async def go():
        async with faults.FaultyClient(jira_client(), "jira", call_timeout=0.3) as c:
            t0 = time.perf_counter()
            out = await c.call("get_ticket", {"ticket_id": "ATL-112"})
            return out, time.perf_counter() - t0

    out, took = run(go())
    assert not out.ok and out.error.startswith("timeout:") and "get_ticket" in out.error
    assert 0.25 < took < 3.0


def test_a_fault_for_another_source_leaves_the_wrapper_alone(monkeypatch):
    monkeypatch.setenv("FAULT_INJECT", "notion:fail,email:timeout")

    async def go():
        async with faults.FaultyClient(jira_client(), "jira") as c:
            return await c.call("get_ticket", {"ticket_id": "ATL-112"})

    assert run(go()).ok


# ---------------------------------------------------------------- through the real agent
def test_agent_survives_an_injected_tool_timeout_and_says_so(monkeypatch):
    """The Phase 1 failure path: a failed call is recorded and the run still finishes with a status."""
    monkeypatch.setenv("FAULT_INJECT", "jira:timeout")
    llm = FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
                  synthesizer=[synth_reply("The ticket could not be read.", [])])
    client = faults.FaultyClient(jira_client(), "jira", call_timeout=0.3)
    r = run(arun_agent("What is the status of the payments ticket?", llm=llm, mcp=client, known_ids=STUB_IDS))
    assert r["status"] in ("ok", "invalid_output", "loop_cap") and r["citations"] == []
    assert [c["name"] for c in r["tool_calls"]] == ["get_ticket"]


def test_agent_stops_cleanly_when_the_only_source_cannot_start(monkeypatch):
    """Phase 1 behaviour for a dead server is status=error (decision 6 changes this only once D's multi-source
    client exists; with one source and no fallback there is nothing else to answer from)."""
    monkeypatch.setenv("FAULT_INJECT", "jira:fail")
    llm = FakeLLM(agent=[text_reply()], synthesizer=[synth_reply("x", [])])
    r = run(arun_agent("anything", llm=llm, mcp=faults.FaultyClient(jira_client(), "jira"), known_ids=STUB_IDS))
    assert r["status"] == "error" and r["citations"] == [] and r["answer"]
    assert llm.count("agent") == 0                                # it never reached the model
