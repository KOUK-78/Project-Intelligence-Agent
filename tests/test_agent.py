"""Agent tests: scripted fake LLM + the REAL MCP client talking to the REAL Jira server over stdio.

No Ollama or GPU needed. Most tests use a 3-ticket fixture (tests/fixtures/jira_stub.json) so
they don't depend on the full dataset; one test runs against the full data when it is present.
Failure tests use tests/bad_server.py (slow / crashing / malformed server).
"""
import asyncio
import functools
import json
from pathlib import Path

import pytest

from app import config, tracing
from app.agent import arun_agent
from app.nodes.agent_node import extract_calls
from app.nodes.llm import LLMReply
from app.nodes.mcp_client import McpToolClient
from app.nodes.tool_node import clean_args, drop_bad_label, guard_filters
from app.nodes.verify import bracket_bare_ids, strip_trailing_id_list, tidy_brackets, trim_not_found_tangent, verify_citations
from tests.fakes import FakeLLM, synth_reply, text_reply, tool_reply

HERE = Path(__file__).parent
BAD_SERVER = HERE / "bad_server.py"
STUB_DATA = HERE / "fixtures" / "jira_stub.json"
STUB_IDS = {"ATL-109", "ATL-112", "ATL-118"}
RESULT_KEYS = {"answer", "citations", "status", "trace_id", "tool_calls", "latency_ms"}


def sync(fn):
    """Run an async test with asyncio.run (keeps pytest-asyncio out of requirements)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def jira(**kw) -> McpToolClient:
    """The real jira_server, pointed at the 3-ticket fixture."""
    return McpToolClient(env={"JIRA_DATA_PATH": str(STUB_DATA)}, **kw)


async def run(llm, question="What is blocking Project Atlas?", **kw):
    kw.setdefault("mcp", jira())
    kw.setdefault("known_ids", STUB_IDS)
    return await arun_agent(question, llm=llm, **kw)


# ---------- happy path & contract shape ----------

@sync
async def test_happy_path_cited_answer_and_frozen_shape():
    llm = FakeLLM(
        agent=[tool_reply("search_tickets", keyword="payments", status="Blocked"), text_reply()],
        synthesizer=[synth_reply("Atlas is blocked by the payments API delay [ATL-112].", ["ATL-112"])],
    )
    r = await run(llm)
    assert set(r) == RESULT_KEYS
    assert r["status"] == "ok"
    assert r["answer"] == "Atlas is blocked by the payments API delay [ATL-112]."
    assert r["citations"] == ["ATL-112"]
    assert [c["name"] for c in r["tool_calls"]] == ["search_tickets"]
    assert r["tool_calls"][0]["args"] == {"keyword": "payments", "status": "Blocked"}
    assert isinstance(r["tool_calls"][0]["latency_ms"], int)
    assert isinstance(r["latency_ms"], int) and r["trace_id"] is None  # runtime.py fills trace_id


@sync
async def test_multi_step_search_then_get_ticket():
    llm = FakeLLM(
        agent=[tool_reply("search_tickets", keyword="atlas", status="Blocked"),
               tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        synthesizer=[synth_reply("It is Blocked [ATL-112].", ["ATL-112"])],
    )
    r = await run(llm, question="What is the status of the payments API ticket?")
    assert r["status"] == "ok" and len(r["tool_calls"]) == 2 and r["citations"] == ["ATL-112"]


# ---------- loop cap ----------

@sync
async def test_loop_cap_returns_loop_cap_and_stops(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "partial_on_cap", True)
    # A real loop keeps trying NEW searches (repeats of a successful call are ended as "done").
    llm = FakeLLM(agent=lambda n: tool_reply("search_tickets", keyword=f"attempt{n}"), synthesizer=[synth_reply("x", [])])
    r = await run(llm)
    assert r["status"] == "loop_cap"
    assert len(r["tool_calls"]) == config.MAX_AGENT_STEPS
    assert llm.count("agent") == config.MAX_AGENT_STEPS + 1  # bounded, never loops forever
    # Evidence was gathered, so the cap yields a flagged partial answer instead of nothing.
    assert r["answer"].startswith("Partial answer") and llm.count("synthesizer") == 1


@sync
async def test_partial_on_cap_is_off_by_default():
    assert config.HEURISTIC_DEFAULTS["partial_on_cap"] is False
    llm = FakeLLM(agent=lambda n: tool_reply("search_tickets", keyword=f"attempt{n}"), synthesizer=[synth_reply("x", [])])
    r = await run(llm)
    assert r["status"] == "loop_cap" and "could not finish" in r["answer"] and llm.count("synthesizer") == 0


# ---------- unknown ticket / no results ----------

@sync
async def test_unknown_ticket_does_not_crash_and_says_so():
    llm = FakeLLM(
        agent=[tool_reply("get_ticket", ticket_id="ATL-999"), text_reply()],
        synthesizer=[synth_reply("Ticket ATL-999 was not found, so I cannot say [ATL-999].", ["ATL-999"])],
    )
    r = await run(llm)
    assert r["status"] == "ok"
    assert r["citations"] == []  # a not_found payload is not evidence of a real ticket
    assert "[unverified: ATL-999]" in r["answer"]
    assert llm.count("synthesizer") == 1  # not_found is a successful call, so it gets explained


@sync
async def test_no_results_search_is_handled():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="zzz-nothing"), text_reply()],
                  synthesizer=[synth_reply("No matching tickets were found.", [])])
    r = await run(llm)
    assert r["status"] == "ok" and r["citations"] == []


# ---------- structured output: retry once, then invalid_output ----------

@sync
async def test_bad_json_retries_once_then_succeeds():
    llm = FakeLLM(
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        synthesizer=[text_reply("not json at all"), synth_reply("Blocked [ATL-112].", ["ATL-112"])],
    )
    r = await run(llm)
    assert r["status"] == "ok" and r["citations"] == ["ATL-112"]
    assert llm.count("synthesizer") == 2


@sync
async def test_bad_json_twice_is_invalid_output_after_exactly_one_retry():
    llm = FakeLLM(
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        synthesizer=[text_reply("nope"), text_reply('{"answer": ""}')],
    )
    r = await run(llm)
    assert r["status"] == "invalid_output" and r["citations"] == []
    assert llm.count("synthesizer") == 2


# ---------- citation verification (plain Python) ----------

@sync
async def test_hallucinated_and_unretrieved_ids_are_dropped_and_flagged():
    llm = FakeLLM(
        agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
        synthesizer=[synth_reply("Blocked [ATL-112]; also see [ATL-777] and [ATL-109].",
                                 ["ATL-112", "ATL-777", "ATL-109"])],
    )
    r = await run(llm)
    assert r["citations"] == ["ATL-112"]
    assert "[unverified: ATL-777]" in r["answer"]  # not in the data
    assert "[unverified: ATL-109]" in r["answer"]  # real, but neither read nor named in anything read


def test_verify_adds_ids_cited_in_text_but_missing_from_list():
    ans, cites, warns = verify_citations("It is late [ATL-112].", [], known_ids=None, ev_ids={"ATL-112"})
    assert cites == ["ATL-112"] and any("data_file_unavailable" in w for w in warns)


# ---------- tool-call parsing, arg cleaning, nudge, unknown tool ----------

def test_extract_calls_falls_back_to_json_in_text():
    reply = LLMReply(content='Sure!\n```json\n{"tool": "list_overdue", "args": {}}\n```')
    assert extract_calls(reply) == [{"name": "list_overdue", "args": {}}]
    reply = LLMReply(content='<tool_call>{"name": "get_ticket", "arguments": {"ticket_id": "ATL-1"}}</tool_call>')
    assert extract_calls(reply) == [{"name": "get_ticket", "args": {"ticket_id": "ATL-1"}}]
    assert extract_calls(LLMReply(content="DONE")) == []


def test_clean_args_drops_junk_and_coerces_booleans():
    schema = {"properties": {"keyword": {"type": "string"},
                             "status": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                             "overdue": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}}}
    out = clean_args({"keyword": "atlas", "status": "null", "overdue": "true", "bogus": 1}, schema)
    assert out == {"keyword": "atlas", "overdue": True}


@sync
async def test_agent_nudged_once_when_it_answers_without_looking_anything_up():
    llm = FakeLLM(agent=[text_reply("Atlas is fine, trust me."), tool_reply("list_overdue"), text_reply()],
                  synthesizer=[synth_reply("Overdue: [ATL-112].", ["ATL-112"])])
    r = await run(llm)
    assert r["status"] == "ok" and [c["name"] for c in r["tool_calls"]] == ["list_overdue"]


@sync
async def test_unknown_tool_name_is_recorded_as_error_and_agent_can_recover():
    llm = FakeLLM(agent=[tool_reply("delete_everything"), tool_reply("list_overdue"), text_reply()],
                  synthesizer=[synth_reply("Overdue [ATL-112].", ["ATL-112"])])
    r = await run(llm)
    assert r["status"] == "ok" and r["citations"] == ["ATL-112"]


@sync
async def test_server_side_input_errors_come_back_as_data_not_crashes():
    # The real server rejects an invalid status with {"error": "invalid_input"}; the agent must survive it.
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="*", status="Exploded"), text_reply()],
                  synthesizer=[synth_reply("That search was invalid, so I cannot say.", [])])
    r = await run(llm)
    assert r["status"] == "ok" and r["citations"] == []


# ---------- failures: never raise, stop cleanly ----------

@sync
async def test_startup_failure_returns_error_status_without_raising():
    r = await arun_agent("q", llm=FakeLLM(agent=[text_reply()], synthesizer=[text_reply()]),
                         mcp=McpToolClient(HERE / "does_not_exist.py", startup_timeout=10))
    assert r["status"] == "error" and "could not start" in r["answer"] and r["tool_calls"] == []
    assert set(r) == RESULT_KEYS


@pytest.mark.parametrize("mode", ["malformed", "crash"])
@sync
async def test_repeated_tool_failures_stop_with_error(mode):
    llm = FakeLLM(agent=[tool_reply("list_overdue")], synthesizer=[synth_reply("x", [])])
    r = await run(llm, mcp=McpToolClient(BAD_SERVER, env={"BAD_MODE": mode}, call_timeout=5))
    assert r["status"] == "error" and "failed repeatedly" in r["answer"]
    assert len(r["tool_calls"]) == 2  # stops after two failed rounds, not after the full cap


@sync
async def test_single_tool_failure_then_agent_gives_up_skips_the_model_guess():
    llm = FakeLLM(agent=[tool_reply("list_overdue"), text_reply()],
                  synthesizer=[synth_reply("guess [ATL-112]", ["ATL-112"])])
    r = await run(llm, mcp=McpToolClient(BAD_SERVER, env={"BAD_MODE": "malformed"}, call_timeout=5))
    assert r["status"] == "ok" and r["citations"] == []
    assert "could not find any ticket evidence" in r["answer"]
    assert llm.count("synthesizer") == 0  # no usable evidence -> deterministic answer, nothing to hallucinate


@sync
async def test_tool_timeout_is_recorded_not_raised():
    llm = FakeLLM(agent=[tool_reply("list_overdue")], synthesizer=[synth_reply("x", [])])
    r = await run(llm, mcp=McpToolClient(BAD_SERVER, env={"BAD_MODE": "slow"}, call_timeout=0.5))
    assert r["status"] == "error" and len(r["tool_calls"]) == 2


@sync
async def test_llm_outage_returns_error_status():
    class Down:
        async def chat(self, *a, **k):
            raise ConnectionError("ollama not running")

    r = await run(Down())
    assert r["status"] == "error" and "language model" in r["answer"]


# ---------- integration with C's tracing + the full dataset ----------

@sync
async def test_node_spans_wrap_the_awaited_node_work(monkeypatch):
    """Each node gets a span that stays open while the node runs (async-aware node_span)."""
    events: list[str] = []

    class FakeSpan:
        def set_attribute(self, *a): ...

    class FakeCM:
        def __init__(self, name): self.name = name
        def __enter__(self): events.append(f"enter {self.name}"); return FakeSpan()
        def __exit__(self, *a): events.append(f"exit {self.name}")

    class FakeTracer:
        def start_as_current_span(self, name): return FakeCM(name)

    monkeypatch.setattr(tracing, "_tracer", FakeTracer())
    llm = FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
                  synthesizer=[synth_reply("Blocked [ATL-112].", ["ATL-112"])])
    r = await run(llm, question="What is the status of ATL-112?")
    assert r["status"] == "ok"
    assert events == [
        "enter node:agent", "exit node:agent", "enter node:tool_executor", "exit node:tool_executor",
        "enter node:agent", "exit node:agent", "enter node:synthesizer", "exit node:synthesizer",
    ]


@pytest.mark.skipif(not config.DATA_PATH.exists(), reason="full dataset not generated yet")
@sync
async def test_full_dataset_overdue_matches_answer_key():
    expected = {"ATL-112", "ATL-109", "ATL-118", "ATL-121", "ATL-127", "ATL-133"}
    ids = set(json.loads(config.DATA_PATH.read_text())[i]["id"] for i in range(45))
    llm = FakeLLM(agent=[tool_reply("list_overdue"), text_reply()],
                  synthesizer=[synth_reply("Six are overdue: " + ", ".join(f"[{i}]" for i in sorted(expected)),
                                           sorted(expected))])
    r = await arun_agent("Which tickets are overdue?", llm=llm, mcp=McpToolClient(), known_ids=ids)
    assert r["status"] == "ok" and set(r["citations"]) == expected


# ---------- small-model guardrails (found on the first real qwen3:8b run) ----------

def test_project_name_or_phrase_is_dropped_from_label():
    assert drop_bad_label({"keyword": "*", "label": "Project Atlas"}) == {"keyword": "*"}
    assert drop_bad_label({"keyword": "*", "label": "atlas"}) == {"keyword": "*"}
    assert drop_bad_label({"keyword": "*", "label": "payments"}) == {"keyword": "*", "label": "payments"}


@sync
async def test_empty_filtered_search_gets_a_hint_and_label_junk_is_not_sent(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "status_sanitize", False)  # here we want the server's own error
    llm = FakeLLM(
        agent=[tool_reply("search_tickets", keyword="x", label="Project Atlas", status="Exploded"),
               tool_reply("search_tickets", keyword="*", status="Blocked"), text_reply()],
        synthesizer=[synth_reply("Blocked [ATL-112].", ["ATL-112"])],
    )
    r = await run(llm)
    assert r["status"] == "ok" and r["citations"] == ["ATL-112"]
    assert "label" not in r["tool_calls"][0]["args"]
    tool_msgs = [m["content"] for m in llm.calls["agent"][1] if m.get("role") == "tool"]
    assert tool_msgs and "invalid_input" in tool_msgs[0]  # server error passed through untouched


@sync
async def test_zero_results_with_filters_adds_hint_for_the_model():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="*", label="nosuchlabel"), text_reply()],
                  synthesizer=[synth_reply("None found.", [])])
    await run(llm, question="Any tickets labelled nosuchlabel?")
    tool_msgs = [m["content"] for m in llm.calls["agent"][1] if m.get("role") == "tool"]
    assert "Do not repeat this search" in tool_msgs[0]


# ---------- fixes from the first eval run (Q4/Q5/Q6/Q9) ----------

def test_bare_ids_are_bracketed_and_existing_brackets_untouched():
    assert bracket_bare_ids("Blocked: ATL-112, ATL-121 and [ATL-136].") == "Blocked: [ATL-112], [ATL-121] and [ATL-136]."
    assert bracket_bare_ids("The screen (ATL-118) is late") == "The screen ([ATL-118]) is late"


@sync
async def test_why_question_with_only_summaries_is_nudged_to_read_the_ticket():
    llm = FakeLLM(
        agent=[tool_reply("search_tickets", keyword="privacy policy"), text_reply(),
               tool_reply("get_ticket", ticket_id="ATL-109"), text_reply()],
        synthesizer=[synth_reply("Waiting on Legal ATL-109.", ["ATL-109"])],
    )
    r = await run(llm, question="Is the overdue privacy policy ticket late because of the payments API?")
    assert [c["name"] for c in r["tool_calls"]] == ["search_tickets", "get_ticket"]
    assert r["status"] == "ok" and r["answer"] == "Waiting on Legal [ATL-109]." and r["citations"] == ["ATL-109"]


@sync
async def test_list_question_is_not_nudged_so_simple_questions_stay_fast():
    llm = FakeLLM(agent=[tool_reply("list_overdue"), text_reply()], synthesizer=[synth_reply("Overdue [ATL-112].", ["ATL-112"])])
    r = await run(llm, question="Which tickets are overdue?")
    assert [c["name"] for c in r["tool_calls"]] == ["list_overdue"]


@sync
async def test_sentence_keyword_with_zero_results_gets_a_words_hint():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="when is the public launch"), text_reply()],
                  synthesizer=[synth_reply("Not found.", [])])
    await run(llm)
    tool_msgs = [m["content"] for m in llm.calls["agent"][1] if m.get("role") == "tool"]
    assert "1-2 distinctive words" in tool_msgs[0]


# ---------- fixes from the second eval run ----------

@sync
async def test_ticket_named_inside_a_read_ticket_is_a_valid_citation():
    # ATL-118's comment says "waiting on the payments API (ATL-112)": citing ATL-112 is grounded.
    llm = FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-118"), text_reply()],
                  synthesizer=[synth_reply("It waits on the payments API [ATL-112], per [ATL-118].", ["ATL-118", "ATL-112"])])
    r = await run(llm, question="What is ATL-118 waiting on?")
    assert r["citations"] == ["ATL-112", "ATL-118"] and "unverified" not in r["answer"]


@sync
async def test_citations_list_cannot_name_ids_the_answer_does_not_use():
    llm = FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
                  synthesizer=[synth_reply("There is no ticket about Salesforce.", ["ATL-112", "ATL-109"])])
    r = await run(llm, question="Status of the Salesforce CRM integration?")
    assert r["status"] == "ok" and r["citations"] == []


@sync
async def test_dependency_question_reads_named_tickets_in_one_extra_round(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "ref_round", True)
    llm = FakeLLM(
        agent=[tool_reply("search_tickets", keyword="*", status="Blocked"),
               tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],  # last reply repeats
        synthesizer=[synth_reply("ATL-112 blocks the checkout screen ATL-118.", [])],
    )
    r = await run(llm)
    names = [(c["name"], c["args"].get("ticket_id")) for c in r["tool_calls"]]
    assert names[:2] == [("search_tickets", None), ("get_ticket", "ATL-112")]
    assert ("get_ticket", "ATL-118") in names[2:]          # followed from ATL-112's comment, by the graph
    assert llm.count("agent") == 4                            # search, get, done -> auto round -> done
    assert r["status"] == "ok" and r["citations"] == ["ATL-112", "ATL-118"]
    assert r["answer"] == "[ATL-112] blocks the checkout screen [ATL-118]."


@sync
async def test_auto_round_does_not_use_up_the_step_cap(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "ref_round", True)
    # The model spends 4 of its 5 steps one call at a time; the automatic reference round must still run.
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="a"), tool_reply("get_ticket", ticket_id="ATL-112"),
                         tool_reply("search_tickets", keyword="b"), tool_reply("search_tickets", keyword="c"), text_reply()],
                  synthesizer=[synth_reply("Blocked [ATL-112] and [ATL-118].", [])])
    r = await run(llm)
    assert r["status"] == "ok" and len(r["tool_calls"]) > config.MAX_AGENT_STEPS - 1
    assert ("get_ticket", "ATL-118") in [(c["name"], c["args"].get("ticket_id")) for c in r["tool_calls"]]


@sync
async def test_no_reference_nudge_for_simple_lookups():
    llm = FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-112"), text_reply()],
                  synthesizer=[synth_reply("Status Blocked [ATL-112].", ["ATL-112"])])
    r = await run(llm, question="What is the status of ATL-112?")
    assert len(r["tool_calls"]) == 1


# ---------- fixes from the third eval run (Q4 loop_cap, Q6 invented filter) ----------

def test_invented_status_and_label_filters_are_dropped_unless_the_question_names_them():
    q = "When is the Atlas public launch?"
    assert guard_filters({"keyword": "launch", "status": "To Do"}, q) == {"keyword": "launch"}
    assert guard_filters({"keyword": "*", "status": "Blocked"}, "Which tickets are currently Blocked?") == {"keyword": "*", "status": "Blocked"}
    assert guard_filters({"keyword": "*", "label": "payments"}, "Which payments tickets slipped?") == {"keyword": "*", "label": "payments"}
    assert guard_filters({"keyword": "*", "label": "security"}, "What is late?") == {"keyword": "*"}


def test_non_id_brackets_become_parentheses():
    assert tidy_brackets("[ATL-112] [days_overdue: 20] and [unverified: ATL-9]") == "[ATL-112] (days_overdue: 20) and [unverified: ATL-9]"
    assert bracket_bare_ids(tidy_brackets("see [ATL-1, ATL-2]")) == "see ([ATL-1], [ATL-2])"


@sync
async def test_invented_status_filter_is_not_sent_to_the_server():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="privacy", status="To Do"), text_reply()],
                  synthesizer=[synth_reply("Found [ATL-109].", ["ATL-109"])])
    r = await run(llm, question="When is the privacy policy due?")
    assert r["tool_calls"][0]["args"] == {"keyword": "privacy"} and r["citations"] == ["ATL-109"]


@sync
async def test_repeating_a_successful_call_ends_the_loop_instead_of_burning_steps():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="privacy")],  # the last reply repeats forever
                  synthesizer=[synth_reply("Due 2026-06-15 [ATL-109].", ["ATL-109"])])
    r = await run(llm, question="Which ticket covers the privacy policy?")
    assert r["status"] == "ok" and len(r["tool_calls"]) == 1 and r["citations"] == ["ATL-109"]
    assert llm.count("agent") == 2


def test_mcp_client_does_not_touch_deprecated_aliases():
    import warnings
    from app.nodes.mcp_client import parse_tool_result
    from mcp import types
    res = types.CallToolResult(content=[types.TextContent(type="text", text='{"count": 0}')])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert parse_tool_result(res)[:2] == (True, {"count": 0})


# ---------- debug log ----------

def test_agent_debug_log_is_opt_in_and_goes_to_stderr(monkeypatch, capsys):
    def fresh():  # a scripted LLM is consumed by one run
        return FakeLLM(agent=[tool_reply("get_ticket", ticket_id="ATL-109"), text_reply()],
                       synthesizer=[synth_reply("Waiting on Legal [ATL-109].", ["ATL-109"])])

    monkeypatch.setattr(config, "AGENT_DEBUG", False)  # do not depend on the shell's AGENT_DEBUG
    q = "What is the status of ATL-109?"
    asyncio.run(run(fresh(), question=q))
    assert "[agent-debug]" not in capsys.readouterr().err
    monkeypatch.setattr(config, "AGENT_DEBUG", True)
    asyncio.run(run(fresh(), question=q))
    out = capsys.readouterr()
    assert "[agent-debug] tool_executor" in out.err and "get_ticket" in out.err and "[agent-debug]" not in out.out


def test_trailing_echo_of_the_citation_list_is_removed_only_when_nothing_is_lost():
    echoed = "[ATL-112] is late. [ATL-121] waits on [ATL-112]. [ATL-112], [ATL-121]"
    assert strip_trailing_id_list(echoed) == "[ATL-112] is late. [ATL-121] waits on [ATL-112]."
    only_there = "Both are late. [ATL-112], [ATL-121]"
    assert strip_trailing_id_list(only_there) == only_there            # IDs not cited earlier: keep them
    assert strip_trailing_id_list("It is blocked [ATL-121].") == "It is blocked [ATL-121]."
    assert strip_trailing_id_list("Done. [ATL-1]") == "Done. [ATL-1]"   # a single trailing cite is a real cite


# ---------- fixes from the fourth eval run (Q6 conflict, Q7 tangent) ----------

@sync
async def test_when_question_reads_every_candidate_ticket_in_one_round(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "candidate_round", True)
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="checkout"), text_reply()],
                  synthesizer=[synth_reply("Two tickets disagree: [ATL-109] and [ATL-118].", [])])
    r = await run(llm, question="When is the checkout work due?")
    ids = [c["args"].get("ticket_id") for c in r["tool_calls"] if c["name"] == "get_ticket"]
    assert sorted(ids) == ["ATL-109", "ATL-112", "ATL-118"] and r["status"] == "ok"
    assert llm.count("agent") == 3                          # search; done -> graph reads all; done


@sync
async def test_non_when_question_does_not_trigger_candidate_reading():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="checkout"), text_reply()],
                  synthesizer=[synth_reply("Checkout tickets: [ATL-109].", ["ATL-109"])])
    r = await run(llm, question="Which tickets are about checkout?")
    assert [c["name"] for c in r["tool_calls"]] == ["search_tickets"]


def test_not_found_tangent_is_trimmed_only_when_narrow_conditions_all_hold():
    tangent = ("There are no tickets related to the Salesforce CRM integration. However, there are other "
               "integration tickets: [ATL-105] is Done and [ATL-119] is Done.")
    assert trim_not_found_tangent(tangent) == "There are no tickets related to the Salesforce CRM integration."
    keep1 = "There are no blocked tickets for Atlas. [ATL-9] is at risk."          # no pivot word: keep
    keep2 = "[ATL-121] is blocked. However, there are other issues [ATL-9]."       # not a not-found opener
    keep3 = "There are no tickets about Salesforce. However, nothing else relates."  # no IDs in remainder
    assert [trim_not_found_tangent(x) for x in (keep1, keep2, keep3)] == [keep1, keep2, keep3]


# ---------- B2: specific-risk heuristics are OFF by default ----------

@sync
async def test_default_run_makes_no_auto_round_for_when_questions():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="checkout"), text_reply()],
                  synthesizer=[synth_reply("Unclear [ATL-109].", [])])
    r = await run(llm, question="When is the checkout work due?")
    assert [c["name"] for c in r["tool_calls"]] == ["search_tickets"]
    assert not config.HEURISTICS["candidate_round"] and not config.HEURISTICS["ref_round"]


@sync
async def test_filter_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setitem(config.HEURISTICS, "filter_guard", False)
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="checkout", status="Blocked"), text_reply()],
                  synthesizer=[synth_reply("x [ATL-109].", [])])
    r = await run(llm, question="Tell me about checkout.")
    assert r["tool_calls"][0]["args"].get("status") == "Blocked"


# ---------- B3: status_sanitize ----------

def test_sanitize_status_keeps_one_valid_status_and_drops_lists_and_inventions():
    from app.nodes.tool_node import sanitize_status
    assert sanitize_status({"keyword": "x", "status": "in progress"}) == {"keyword": "x", "status": "in progress"}
    assert sanitize_status({"keyword": "x", "status": "To Do, In Progress, Blocked"}) == {"keyword": "x"}
    assert sanitize_status({"keyword": "x", "status": "Open"}) == {"keyword": "x"}
    assert sanitize_status({"keyword": "x"}) == {"keyword": "x"}


@sync
async def test_status_list_from_the_model_is_dropped_before_the_call():
    llm = FakeLLM(agent=[tool_reply("search_tickets", keyword="*", status="To Do, Blocked"), text_reply()],
                  synthesizer=[synth_reply("Not done: [ATL-212].", ["ATL-212"])])
    r = await run(llm, question="Which tickets are not done yet?")
    assert "status" not in r["tool_calls"][0]["args"]
