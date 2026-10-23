"""B3/B6/B7: the planned graph end to end with a scripted LLM and a fake three-source MCP client.

plan -> scoped retrieval loop -> evidence check -> (rewrite once) -> synthesis, plus both step caps,
the planner fallback, partial availability, token accounting, and planner-off equals Phase 1.
"""
import asyncio
import functools

import pytest

from app import config
from app.agent import arun_agent
from app.graph import build_graph
from app.nodes.llm import LLMReply
from app.nodes.mcp_client import ToolOutcome
from tests.fakes import FakeLLM, synth_reply, text_reply, tool_reply
from tests.phase2_fakes import (ALL_TOOLS, SOURCES, FakeMcp, SpyLLM, check_reply, plan_reply, rewrite_reply, script_once)

IDS = {"ATL-001", "NTN-001", "EML-001"}
PHASE1_KEYS = {"answer", "citations", "status", "trace_id", "tool_calls", "latency_ms"}
JIRA_TOOLS = ["search_tickets", "get_ticket", "list_overdue"]
NOTION_TOOLS = ["search_pages", "get_page"]
EMAIL_TOOLS = ["search_emails", "get_email", "get_thread"]


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(autouse=True)
def planner_on(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)


async def run(llm, mcp=None, sources=SOURCES, question="What do the tracker, wiki and mail say?"):
    return await arun_agent(question, llm=llm, mcp=mcp or FakeMcp(), known_ids=IDS, sources=sources)


def one_round(tool, **args):
    """What the agent does for one sub-question: call one tool, then say DONE."""
    return [tool_reply(tool, **args), text_reply()]


THREE = [*one_round("search_tickets", keyword="a"), *one_round("search_pages", keyword="b"),
         *one_round("search_emails", keyword="c")]


# ---------------------------------------------------------------- the whole pipeline

@sync
async def test_full_pipeline_over_three_sources():
    llm = SpyLLM(
        planner=[plan_reply(("jira", "find the ticket"), ("notion", "find the page"), ("email", "find the mail"))],
        agent=THREE, evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-001], page [NTN-001], mail [EML-001].", ["ATL-001", "NTN-001", "EML-001"])],
    )
    mcp = FakeMcp()
    r = await run(llm, mcp)
    assert r["status"] == "ok" and r["citations"] == ["ATL-001", "NTN-001", "EML-001"]
    assert set(r) == PHASE1_KEYS | {"plan", "sources"}  # no token_usage: this fake reports no tokens
    assert r["sources"] == ["jira", "notion", "email"]
    assert [c["name"] for c in r["tool_calls"]] == ["search_tickets", "search_pages", "search_emails"]
    assert [p["status"] for p in r["plan"]] == ["resolved"] * 3
    assert [(p["id"], p["source"], p["steps"], p["attempts"]) for p in r["plan"]] == [
        ("sq1", "jira", 1, 1), ("sq2", "notion", 1, 1), ("sq3", "email", 1, 1)]
    assert llm.count("planner") == 1 and llm.count("evidence_check") == 3 and llm.count("synthesizer") == 1
    assert "rewrite" not in llm.calls  # nothing needed rewriting


@sync
async def test_each_retrieval_loop_sees_only_its_sources_tools_and_its_own_sub_question():
    llm = SpyLLM(
        planner=[plan_reply(("jira", "find the ticket"), ("notion", "find the page"), ("email", "find the mail"))],
        agent=THREE, evidence_check=[check_reply(True)], synthesizer=[synth_reply("x [ATL-001]", ["ATL-001"])],
    )
    await run(llm)
    assert llm.tools_seen["agent"] == [JIRA_TOOLS] * 2 + [NOTION_TOOLS] * 2 + [EMAIL_TOOLS] * 2
    firsts = [m[1]["content"] for m in llm.calls["agent"] if len(m) == 2]  # the first decision of each loop
    assert firsts == ["find the ticket", "find the page", "find the mail"]  # fresh history, the sub-question only


@sync
async def test_a_tool_outside_the_scope_is_refused_and_never_reaches_the_server():
    llm = FakeLLM(
        planner=[plan_reply(("notion", "find the page"))],
        agent=[tool_reply("search_tickets", keyword="x"), *one_round("search_pages", keyword="y")],
        evidence_check=[check_reply(True)], synthesizer=[synth_reply("p [NTN-001]", ["NTN-001"])],
    )
    mcp = FakeMcp()
    r = await run(llm, mcp)
    assert [name for name, _ in mcp.calls] == ["search_pages"]
    assert r["citations"] == ["NTN-001"]


# ---------------------------------------------------------------- step caps

@sync
async def test_per_sub_question_cap_stops_each_loop_and_the_next_one_starts_fresh(monkeypatch):
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 2)
    monkeypatch.setattr(config, "MAX_TOTAL_STEPS", 20)
    llm = FakeLLM(
        planner=[plan_reply(("jira", "first"), ("jira", "second"))],
        agent=lambda n: tool_reply("search_tickets", keyword=f"k{n}"),  # never says done
        evidence_check=[check_reply(True)], synthesizer=[synth_reply("t [ATL-001]", ["ATL-001"])],
    )
    r = await run(llm)
    assert len(r["tool_calls"]) == 4  # 2 rounds each: the second loop got its own full cap (flags and steps reset)
    assert llm.count("agent") == 6  # cap + 1 final decision, per sub-question
    assert [(p["steps"], p["capped"], p["status"]) for p in r["plan"]] == [(2, True, "resolved")] * 2
    assert r["status"] == "ok"  # a capped loop that found enough is not a failed run


@sync
async def test_total_cap_bounds_the_run_and_skips_what_is_left(monkeypatch):
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 5)
    monkeypatch.setattr(config, "MAX_TOTAL_STEPS", 3)
    llm = FakeLLM(
        planner=[plan_reply(("jira", "first"), ("notion", "second"), ("email", "third"))],
        agent=lambda n: tool_reply("search_tickets", keyword=f"k{n}"),
        evidence_check=[check_reply(True)], synthesizer=[synth_reply("t [ATL-001]", ["ATL-001"])],
    )
    r = await run(llm)
    assert len(r["tool_calls"]) == 3 <= config.MAX_TOTAL_STEPS
    assert llm.count("agent") == 4  # no model call at all for the skipped sub-questions
    assert [p["status"] for p in r["plan"]] == ["resolved", "unresolved", "unresolved"]
    assert [p["outcome"] for p in r["plan"][1:]] == ["budget_exhausted"] * 2
    assert [p["steps"] for p in r["plan"]] == [3, 0, 0]
    assert r["status"] == "loop_cap" and "Not enough evidence for" in r["answer"] and r["citations"] == ["ATL-001"]


@sync
async def test_the_rewrite_spends_from_the_same_total_budget(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOTAL_STEPS", 1)
    empty = {"search_tickets": {"count": 0, "results": []}}
    llm = FakeLLM(planner=[plan_reply(("jira", "first"))], agent=one_round("search_tickets", keyword="a"),
                  rewrite=[rewrite_reply("other words")], synthesizer=[synth_reply("x", [])])
    r = await run(llm, FakeMcp(results=empty))
    assert llm.count("rewrite") == 0 and len(r["tool_calls"]) == 1  # budget gone: no rewrite, no second attempt
    assert r["plan"][0]["outcome"] == "budget_exhausted" and r["status"] == "loop_cap"


def test_the_outer_recursion_limit_covers_the_worst_case_plan():
    worst = 2 + 2 * (1 + config.MAX_REWRITES_PER_SUBQUESTION) * config.MAX_SUBQUESTIONS  # planner + (retrieve+check)* + synth
    assert worst < config.GRAPH_RECURSION_LIMIT


# ---------------------------------------------------------------- evidence check and rewrite, in the graph

@sync
async def test_empty_search_is_rewritten_once_and_the_second_try_can_succeed():
    def search(name, args):
        return {"count": 0, "results": []} if args["keyword"] == "first" else {"count": 1, "results": [{"id": "ATL-001"}]}

    llm = SpyLLM(
        planner=[plan_reply(("jira", "find the ticket"))],
        agent=[*one_round("search_tickets", keyword="first"), *one_round("search_tickets", keyword="second")],
        rewrite=[rewrite_reply("find it another way")], evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Found [ATL-001].", ["ATL-001"])],
    )
    r = await run(llm, FakeMcp(results={"search_tickets": search}))
    p = r["plan"][0]
    assert (p["status"], p["attempts"], p["steps"], p["rewritten"]) == ("resolved", 2, 2, "find it another way")
    assert llm.count("rewrite") == 1 and llm.count("evidence_check") == 1  # code judged the empty try; the model the second
    assert llm.calls["agent"][2][1]["content"] == "find it another way"  # the rewritten query, with a clean history
    assert r["citations"] == ["ATL-001"]


@sync
async def test_still_nothing_after_the_rewrite_says_not_enough_evidence_without_guessing():
    empty = {"search_tickets": {"count": 0, "results": []}}
    llm = FakeLLM(planner=[plan_reply(("jira", "first"))],
                  agent=[*one_round("search_tickets", keyword="a"), *one_round("search_tickets", keyword="b")],
                  rewrite=[rewrite_reply("other words")], synthesizer=[synth_reply("never used", [])])
    r = await run(llm, FakeMcp(results=empty))
    assert llm.count("rewrite") == 1 and llm.count("synthesizer") == 0  # one rewrite, never a second
    assert r["answer"] == "There is not enough evidence to answer that." and r["citations"] == [] and r["status"] == "ok"
    assert r["plan"][0]["status"] == "unresolved" and r["plan"][0]["attempts"] == 2 and r["sources"] == []


# ---------------------------------------------------------------- partial availability

@sync
async def test_a_source_marked_down_is_reported_and_never_called():
    down = [s if s.name != "email" else type(s)(s.name, s.description, s.tools, available=False) for s in SOURCES]
    llm = FakeLLM(
        planner=[plan_reply(("jira", "find the ticket"), ("email", "find the mail"))],
        agent=one_round("search_tickets", keyword="a"), evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Ticket [ATL-001].", ["ATL-001"])],
    )
    mcp = FakeMcp()
    r = await run(llm, mcp, sources=down)
    assert [name for name, _ in mcp.calls] == ["search_tickets"] and llm.count("agent") == 2
    assert [p["status"] for p in r["plan"]] == ["resolved", "unavailable"] and r["plan"][1]["unavailable"] == ["email"]
    assert r["answer"] == "Ticket [ATL-001]. Source unavailable: email." and r["sources"] == ["jira"]
    assert r["citations"] == ["ATL-001"] and r["status"] == "ok"
    assert "SOURCE UNAVAILABLE: email" in llm.calls["synthesizer"][0][1]["content"]


@sync
async def test_a_source_whose_tools_are_not_advertised_is_unavailable():
    live = [t for t in ALL_TOOLS if t["name"] in JIRA_TOOLS]
    llm = FakeLLM(planner=[plan_reply(("notion", "find the page"))], synthesizer=[synth_reply("never", [])],
                  agent=[text_reply()])
    r = await run(llm, FakeMcp(tools=live))
    assert llm.count("agent") == 0 and r["plan"][0]["status"] == "unavailable"
    assert r["answer"] == "There is not enough evidence to answer that. Source unavailable: notion."


@sync
async def test_a_source_that_fails_at_run_time_does_not_stop_the_others():
    dead = ToolOutcome(False, None, "call_failed: server went away", 1)
    llm = FakeLLM(
        planner=[plan_reply(("notion", "find the page"), ("jira", "find the ticket"))],
        agent=lambda n: tool_reply("search_pages", keyword=f"k{n}") if n < 3 else (
            tool_reply("search_tickets", keyword="t") if n == 3 else text_reply()),
        evidence_check=[check_reply(True)], synthesizer=[synth_reply("Ticket [ATL-001].", ["ATL-001"])],
    )
    r = await run(llm, FakeMcp(results={"search_pages": dead}))
    assert [p["status"] for p in r["plan"]] == ["unavailable", "resolved"]
    assert r["plan"][0]["outcome"] in ("tool_failures", "unreachable") and r["plan"][0]["unavailable"] == ["notion"]
    assert "source unavailable" in r["answer"].lower() and r["citations"] == ["ATL-001"]
    assert llm.count("rewrite") == 0  # rewriting does not fix a dead server


@sync
async def test_an_unreachable_language_model_stops_the_run_with_status_error():
    class Boom:
        async def chat(self, job, *a, **k):
            if job == "planner":
                return plan_reply(("jira", "find the ticket"))
            raise ConnectionError("ollama is down")

    r = await run(Boom())
    assert r["status"] == "error" and "language model" in r["answer"]
    assert [p["status"] for p in r["plan"]] == ["not_run"]  # the plan is still reported


# ---------------------------------------------------------------- planner fallback, end to end

@sync
async def test_a_malformed_plan_falls_back_to_one_loop_over_every_tool():
    llm = SpyLLM(
        planner=[LLMReply(content="nope"), LLMReply(content="still nope")],
        agent=[*one_round("search_pages", keyword="a")], evidence_check=[check_reply(True)],
        synthesizer=[synth_reply("Page [NTN-001].", ["NTN-001"])],
    )
    r = await run(llm)
    assert llm.count("planner") == 2
    assert llm.tools_seen["agent"][0] == [t["name"] for t in ALL_TOOLS]  # Phase 1 behaviour: everything on offer
    assert [(p["id"], p["source"]) for p in r["plan"]] == [("sq1", "all")]
    assert r["citations"] == ["NTN-001"] and r["sources"] == ["notion"]  # credited to the tool's real source
    assert llm.calls["agent"][0][1]["content"] == "What do the tracker, wiki and mail say?"  # the original question


@sync
async def test_without_a_registry_the_planner_is_skipped_and_everything_is_in_scope():
    llm = SpyLLM(planner=[LLMReply(content="never called")], agent=one_round("get_ticket", ticket_id="ATL-001"),
                 evidence_check=[check_reply(True)], synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])
    r = await run(llm, sources=None)
    assert llm.count("planner") == 0 and r["plan"][0]["source"] == "all" and r["citations"] == ["ATL-001"]


# ---------------------------------------------------------------- accounting (B6)

@sync
async def test_token_usage_is_summed_per_job():
    llm = SpyLLM(
        usage={"planner": (100, 10), "agent": (50, 5), "evidence_check": (30, 3), "synthesizer": (200, 20)},
        planner=[plan_reply(("jira", "find the ticket"))], agent=one_round("search_tickets", keyword="a"),
        evidence_check=[check_reply(True)], synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])],
    )
    u = (await run(llm))["token_usage"]
    assert u["calls"] == 5 and u["prompt_tokens"] == 100 + 2 * 50 + 30 + 200 and u["completion_tokens"] == 10 + 2 * 5 + 3 + 20
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]
    assert u["by_job"]["agent"] == {"prompt_tokens": 100, "completion_tokens": 10, "calls": 2, "total_tokens": 110}
    assert set(u["by_job"]) == {"planner", "agent", "evidence_check", "synthesizer"}


@sync
async def test_token_usage_counts_only_this_run_when_the_model_is_reused():
    def fresh():
        return dict(planner=[plan_reply(("jira", "q"))], agent=one_round("search_tickets", keyword="a"),
                    evidence_check=[check_reply(True)], synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])

    llm = SpyLLM(usage={"planner": (10, 1), "agent": (10, 1), "evidence_check": (10, 1), "synthesizer": (10, 1)}, **fresh())
    first = await run(llm)
    llm.script, llm.calls = fresh(), {}
    second = await run(llm)
    assert first["token_usage"]["calls"] == second["token_usage"]["calls"] == 5
    assert second["token_usage"]["total_tokens"] == first["token_usage"]["total_tokens"]


# ---------------------------------------------------------------- planner off == Phase 1

def test_planner_off_builds_exactly_the_phase1_graph(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    nodes = set(build_graph(FakeLLM(), FakeMcp(), None, SOURCES).get_graph().nodes)
    assert nodes == {"__start__", "agent", "tool_executor", "synthesizer", "__end__"}
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)
    assert {"planner", "retrieve", "evidence_check"} <= set(build_graph(FakeLLM(), FakeMcp(), None, SOURCES).get_graph().nodes)


@sync
async def test_planner_off_never_calls_a_phase2_job_and_returns_only_phase1_keys(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    llm = SpyLLM(agent=[tool_reply("search_tickets", keyword="a"), text_reply()],
                 synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])  # no planner/check/rewrite script at all
    r = await run(llm)
    assert set(r) == PHASE1_KEYS and r["citations"] == ["ATL-001"] and r["status"] == "ok"
    assert set(llm.calls) == {"agent", "synthesizer"}
    assert llm.tools_seen["agent"][0] == [t["name"] for t in ALL_TOOLS]  # the registry is ignored: all tools, as before


@sync
async def test_planner_off_gives_the_same_result_with_or_without_a_registry(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)

    def script():
        return dict(agent=[tool_reply("search_tickets", keyword="a"), tool_reply("get_ticket", ticket_id="ATL-001"),
                           text_reply()], synthesizer=[synth_reply("T [ATL-001].", ["ATL-001"])])

    a = await run(FakeLLM(**script()), sources=SOURCES)
    b = await run(FakeLLM(**script()), sources=None)
    for r in (a, b):
        r.pop("latency_ms")
        for call in r["tool_calls"]:
            call.pop("latency_ms")
    assert a == b


@sync
async def test_planner_off_loop_cap_is_still_the_phase1_behaviour(monkeypatch):
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)
    llm = FakeLLM(agent=lambda n: tool_reply("search_tickets", keyword=f"k{n}"), synthesizer=[synth_reply("x", [])])
    r = await run(llm)
    assert r["status"] == "loop_cap" and len(r["tool_calls"]) == config.MAX_AGENT_STEPS
    assert llm.count("agent") == config.MAX_AGENT_STEPS + 1 and "could not finish" in r["answer"]
