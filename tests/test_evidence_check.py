"""B4/B7: evidence check. Code verdicts first, then the model; one rewrite at most; hard caps on every path."""
import asyncio

import pytest

from app import config
from app.nodes.evidence_check import CHECK_SYSTEM, REWRITE_SYSTEM, code_verdict, describe_tried, make_evidence_check_node
from app.nodes.llm import LLMReply
from tests.fakes import FakeLLM
from tests.phase2_fakes import check_reply, rewrite_reply

PLAN = [
    {"id": "sq1", "question": "first question", "source": "jira", "reason": ""},
    {"id": "sq2", "question": "second question", "source": "notion", "reason": ""},
]


def ev(ok=True, data=None, error=None, sq="sq1", source="jira", tool="search_tickets", args=None):
    data = {"id": "ATL-001"} if ok and data is None else data
    return {"tool": tool, "args": args or {"keyword": "k"}, "ok": ok, "data": data, "error": error, "sq": sq, "source": source}


def state(evidence, inner="ok", attempt=0, idx=0, total=0, steps=1, question=None, plan=PLAN):
    return {"plan": plan, "sq_index": idx, "sq_attempt": attempt, "sq_steps": steps, "total_steps": total,
            "sq_inner_status": inner, "sq_question": question or plan[idx]["question"], "evidence": evidence}


def check(llm, st, down=None):
    return asyncio.run(make_evidence_check_node(llm, down)(st))


def llm_with(**script):
    script.setdefault("evidence_check", [check_reply(True)])
    script.setdefault("rewrite", [rewrite_reply("a different query")])
    return FakeLLM(**script)


def calls(llm):
    return llm.count("evidence_check"), llm.count("rewrite")


def record(out):
    assert len(out["sq_results"]) == 1
    return out["sq_results"][0]


# ---------------------------------------------------------------- code verdicts (no model)

def test_nothing_came_back_is_insufficient_without_asking_the_model():
    assert code_verdict([], "ok").reason == "no_successful_evidence"
    assert code_verdict([ev(ok=False, error="tool_error: bad args")], "ok").status == "insufficient"


def test_calls_that_returned_no_record_are_insufficient():
    assert code_verdict([ev(data={"count": 0, "results": []})], "ok").reason == "no_records"
    assert code_verdict([ev(data={"error": "not_found", "id": "ATL-999"})], "ok").reason == "no_records"


def test_a_source_that_is_down_is_unavailable_not_insufficient():
    assert code_verdict([], "unavailable").status == "unavailable"
    assert code_verdict([ev(ok=False, error="tool_error: x")], "error").reason == "tool_failures"
    for prefix in ("not_connected: x", "timeout: x", "call_failed: x", "source_unavailable: x"):
        assert code_verdict([ev(ok=False, error=prefix)], "ok").reason == "unreachable", prefix


def test_real_records_go_to_the_model():
    assert code_verdict([ev()], "ok") is None
    assert code_verdict([ev(), ev(ok=False, error="timeout: x")], "error") is None  # some evidence survived


# ---------------------------------------------------------------- sufficient / model verdict

def test_sufficient_evidence_resolves_and_moves_to_the_next_sub_question():
    llm = llm_with()
    out = check(llm, state([ev()], steps=2))
    assert calls(llm) == (1, 0)
    assert record(out) == {"id": "sq1", "question": "first question", "source": "jira", "status": "resolved",
                           "reason": "model", "steps": 2, "attempts": 1, "rewritten": None, "capped": False,
                           "unavailable": []}
    assert out["route"] == "retrieve" and out["sq_index"] == 1 and out["sq_attempt"] == 0 and out["sq_steps"] == 0
    assert out["sq_question"] == "second question" and out["sq_inner_status"] == ""


def test_the_last_sub_question_routes_to_the_synthesizer():
    out = check(llm_with(), state([ev(sq="sq2", source="notion")], idx=1))
    assert out["route"] == "synthesizer" and out["sq_index"] == 2 and record(out)["id"] == "sq2"


def test_the_check_judges_only_this_sub_questions_evidence_against_the_original_wording():
    llm = llm_with()
    other = ev(sq="sq2", source="notion", data={"id": "NTN-777"}, tool="get_page")
    check(llm, state([ev(), other], question="a rewritten form"))
    system, user = llm.calls["evidence_check"][0]
    assert system["content"] == CHECK_SYSTEM
    assert "first question" in user["content"] and "a rewritten form" not in user["content"]
    assert "ATL-001" in user["content"] and "NTN-777" not in user["content"]


def test_a_capped_attempt_with_enough_evidence_still_resolves_and_is_flagged():
    assert record(check(llm_with(), state([ev()], inner="loop_cap")))["capped"] is True


# ---------------------------------------------------------------- rewrite exactly once

def test_empty_evidence_triggers_one_rewrite_with_no_check_call():
    llm = llm_with()
    out = check(llm, state([], total=3))
    assert calls(llm) == (0, 1)
    assert out["route"] == "retrieve" and out["sq_attempt"] == 1 and out["sq_question"] == "a different query"
    assert "sq_results" not in out and out["sq_inner_status"] == ""  # nothing committed yet; same sub-question
    system, user = llm.calls["rewrite"][0]
    assert system["content"] == REWRITE_SYSTEM and "first question" in user["content"]


def test_the_rewrite_prompt_lists_what_was_already_tried():
    llm = llm_with()
    check(llm, state([ev(data={"count": 0, "results": []}, args={"keyword": "zzz"})]))
    user = llm.calls["rewrite"][0][1]["content"]
    assert 'search_tickets({"keyword":"zzz"}) -> 0 results' in user


def test_model_says_insufficient_then_rewrite_carries_what_is_missing():
    llm = llm_with(evidence_check=[check_reply(False, "the owner")])
    out = check(llm, state([ev()]))
    assert calls(llm) == (1, 1) and out["route"] == "retrieve"
    assert "the owner" in llm.calls["rewrite"][0][1]["content"]


def test_after_the_one_rewrite_an_insufficient_sub_question_is_unresolved_and_never_rewritten_again():
    llm = llm_with(evidence_check=[check_reply(False)])
    out = check(llm, state([ev()], attempt=1, question="a different query", steps=3))
    assert calls(llm) == (1, 0)
    r = record(out)
    assert r["status"] == "unresolved" and r["reason"] == "model" and r["attempts"] == 2
    assert r["rewritten"] == "a different query" and r["steps"] == 3
    assert out["route"] == "retrieve" and out["sq_index"] == 1  # moves on


def test_no_evidence_after_the_rewrite_is_unresolved_without_any_model_call():
    llm = llm_with()
    out = check(llm, state([], attempt=1), )
    assert calls(llm) == (0, 0) and record(out)["status"] == "unresolved"
    assert record(out)["reason"] == "no_successful_evidence"


def test_rewriting_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_REWRITES_PER_SUBQUESTION", 0)
    llm = llm_with()
    out = check(llm, state([]))
    assert calls(llm) == (0, 0) and record(out)["status"] == "unresolved"


def test_no_rewrite_when_the_step_budget_is_spent():
    llm = llm_with()
    out = check(llm, state([], total=config.MAX_TOTAL_STEPS))
    assert calls(llm) == (0, 0)
    assert record(out)["status"] == "unresolved" and record(out)["reason"] == "budget_exhausted"


def test_a_rewrite_that_repeats_the_question_or_is_malformed_is_dropped():
    same = llm_with(rewrite=[rewrite_reply("FIRST question")])
    assert record(check(same, state([])))["reason"] == "rewrite_invalid"
    junk = llm_with(rewrite=[LLMReply(content="nope"), LLMReply(content="still nope")])
    out = check(junk, state([]))
    assert junk.count("rewrite") == 2 and record(out)["reason"] == "rewrite_invalid"  # one retry, no more


# ---------------------------------------------------------------- unavailable / budget

def test_unavailable_source_commits_without_calling_the_model():
    llm = llm_with()
    out = check(llm, state([], inner="unavailable"))
    assert calls(llm) == (0, 0)
    assert record(out)["status"] == "unavailable" and record(out)["unavailable"] == ["jira"]
    assert out["route"] == "retrieve" and out["sq_index"] == 1


def test_all_scope_reports_the_sources_that_failed_or_are_down():
    plan = [{"id": "sq1", "question": "q", "source": "all", "reason": ""}]
    failed = ev(ok=False, error="timeout: x", source="email", tool="search_emails")
    out = check(llm_with(), state([failed], plan=plan), down={"notion"})
    assert record(out)["status"] == "unavailable" and record(out)["unavailable"] == ["email", "notion"]
    ok = check(llm_with(), state([ev()], plan=plan), down={"notion"})
    assert record(ok)["status"] == "resolved" and record(ok)["unavailable"] == ["notion"]  # still worth saying


def test_a_skipped_retrieval_is_unresolved_for_budget_with_no_calls():
    llm = llm_with()
    out = check(llm, state([], inner="budget", steps=0))
    assert calls(llm) == (0, 0)
    assert record(out)["status"] == "unresolved" and record(out)["reason"] == "budget_exhausted"


# ---------------------------------------------------------------- failure paths

def test_a_bad_check_reply_is_retried_once_then_fails_open_with_a_warning():
    llm = llm_with(evidence_check=[LLMReply(content="?"), LLMReply(content="??")])
    out = check(llm, state([ev()]))
    assert llm.count("evidence_check") == 2 and record(out)["status"] == "resolved"
    assert record(out)["reason"] == "check_invalid" and any("evidence_check_invalid" in w for w in out["warnings"])


def test_a_bad_check_reply_then_a_good_one_is_used():
    llm = llm_with(evidence_check=[LLMReply(content="?"), check_reply(False)])
    assert check(llm, state([ev()]))["route"] == "retrieve"  # insufficient -> rewrite


@pytest.mark.parametrize("script", [{"evidence_check": None}, {"rewrite": None}])
def test_an_unreachable_model_stops_the_run_with_status_error(script):
    class Boom:
        async def chat(self, *a, **k):
            raise ConnectionError("down")

    st = state([ev()]) if "evidence_check" in script else state([])
    out = check(Boom(), st)
    assert out["route"] == "end" and out["status"] == "error" and out["errors"][0].startswith("llm_unavailable")
    assert "language model" in out["answer"]


def test_describe_tried_handles_failures_and_plain_results():
    text = describe_tried([ev(ok=False, error="timeout: slow"), ev(data={"id": "ATL-001"}), ev(data={"count": 2})])
    assert "failed (timeout: slow)" in text and "-> ok" in text and "-> 2 results" in text
    assert describe_tried([]) == "- nothing"


# ---------------------------------------------------------------- servers that cannot read their data

DOWN = {"error": "data_unavailable", "message": "could not read data"}


def test_a_server_that_answers_data_unavailable_is_unavailable_not_insufficient():
    verdict = code_verdict([ev(data=DOWN)], "ok")
    assert verdict.status == "unavailable" and verdict.reason == "data_unavailable"
    assert code_verdict([ev(data=DOWN), ev(data=DOWN)], "ok").reason == "data_unavailable"
    assert code_verdict([ev(data=DOWN), ev(ok=False, error="timeout: x")], "ok").reason == "unreachable"


def test_data_unavailable_never_hides_real_records_or_an_ordinary_empty_result():
    assert code_verdict([ev(data=DOWN), ev()], "ok") is None  # a real record still goes to the model
    assert code_verdict([ev(data=DOWN), ev(data={"count": 0, "results": []})], "ok").reason == "no_records"
    assert code_verdict([ev(data={"error": "invalid_input", "message": "bad"})], "ok").reason == "no_records"
    assert code_verdict([ev(data={"error": "not_found", "id": "X-1"})], "ok").status == "insufficient"


def test_data_unavailable_commits_without_a_rewrite_or_a_model_call():
    llm = llm_with()
    out = check(llm, state([ev(data=DOWN)]))
    assert calls(llm) == (0, 0)
    assert record(out)["status"] == "unavailable" and record(out)["reason"] == "data_unavailable"
    assert record(out)["unavailable"] == ["jira"]
