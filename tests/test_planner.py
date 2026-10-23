"""B2/B7: planner node. Valid plans, and every failure path ending in the Phase 1 fallback."""
import asyncio
import json

from app import config
from app.nodes.llm import LLMReply
from app.nodes.planner import PLANNER_SYSTEM, make_planner_node, plan_schema, validate_plan
from app.nodes.scope import ALL_SOURCES
from tests.fakes import FakeLLM
from tests.phase2_fakes import SOURCES, plan_reply

NAMES = [s.name for s in SOURCES]


def run_planner(llm, question="Q?", sources=SOURCES):
    return asyncio.run(make_planner_node(llm, sources)({"question": question}))


def is_fallback(out, question="Q?"):
    return out["plan"] == [{"id": "sq1", "question": question, "source": ALL_SOURCES, "reason": out["plan"][0]["reason"]}]


def bad(content="not json"):
    return LLMReply(content=content)


def test_valid_plan_is_normalised_and_starts_the_loop():
    llm = FakeLLM(planner=[plan_reply(("Jira", "  find   the   ticket "), ("notion", "find the page"))])
    out = run_planner(llm)
    assert out["plan"] == [
        {"id": "sq1", "question": "find the ticket", "source": "jira", "reason": "r"},  # ids reassigned, source canonical
        {"id": "sq2", "question": "find the page", "source": "notion", "reason": "r"},
    ]
    assert out["route"] == "retrieve" and out["sq_index"] == 0 and out["sq_attempt"] == 0
    assert out["sq_question"] == "find the ticket" and out["total_steps"] == 0 and out["warnings"] == []
    assert llm.count("planner") == 1


def test_prompt_lists_sources_and_cap_but_no_data_and_schema_pins_the_sources():
    llm = FakeLLM(planner=[plan_reply(("jira", "a"))])
    run_planner(llm, "the question")
    system, user = llm.calls["planner"][0]
    assert system["content"] == PLANNER_SYSTEM
    for s in SOURCES:
        assert f"- {s.name}: {s.description}" in user["content"]
    assert str(config.MAX_SUBQUESTIONS) in user["content"] and "the question" in user["content"]
    schema = plan_schema(NAMES)
    item = schema["properties"]["sub_questions"]["items"]
    assert item["properties"]["source"]["enum"] == NAMES
    assert schema["properties"]["sub_questions"]["maxItems"] == config.MAX_SUBQUESTIONS


def test_planner_runs_cold_and_without_thinking():
    assert config.TEMPERATURE == 0.1
    assert config.THINKING["planner"] is False and "planner" in config.MODELS


def test_malformed_twice_falls_back_after_exactly_one_retry():
    llm = FakeLLM(planner=[bad(), bad()])
    out = run_planner(llm)
    assert is_fallback(out) and llm.count("planner") == 2
    assert out["warnings"] == ["planner_fallback:invalid_json"]
    assert out["route"] == "retrieve" and out["sq_question"] == "Q?"


def test_malformed_then_valid_uses_the_retry_and_tells_the_model_what_was_wrong():
    llm = FakeLLM(planner=[bad(), plan_reply(("email", "find the thread"))])
    out = run_planner(llm)
    assert out["plan"][0]["source"] == "email" and out["warnings"] == [] and llm.count("planner") == 2
    retry = llm.calls["planner"][1]
    assert retry[-2] == {"role": "assistant", "content": "not json"}
    assert "sub_questions" in retry[-1]["content"]


def test_unknown_source_is_retried_then_falls_back():
    llm = FakeLLM(planner=[plan_reply(("github", "x")), plan_reply(("github", "x"))])
    out = run_planner(llm)
    assert is_fallback(out) and llm.count("planner") == 2
    assert out["warnings"] == ["planner_fallback:unknown_source"]
    assert "jira, notion, email" in llm.calls["planner"][1][-1]["content"]  # the retry names the valid sources


def test_unknown_source_then_valid_recovers():
    llm = FakeLLM(planner=[plan_reply(("github", "x")), plan_reply(("jira", "y"))])
    out = run_planner(llm)
    assert out["plan"][0]["source"] == "jira" and out["warnings"] == []


def test_empty_plan_falls_back():
    empty = LLMReply(content=json.dumps({"sub_questions": []}))
    llm = FakeLLM(planner=[empty, empty])
    out = run_planner(llm)
    assert is_fallback(out) and out["warnings"] == ["planner_fallback:empty_plan"] and llm.count("planner") == 2


def test_blank_sub_question_is_invalid():
    llm = FakeLLM(planner=[plan_reply(("jira", "   ")), plan_reply(("jira", "  "))])
    assert run_planner(llm)["warnings"] == ["planner_fallback:empty_question"]


def test_too_many_sub_questions_are_cut_to_the_cap_without_a_retry():
    n = config.MAX_SUBQUESTIONS + 3
    llm = FakeLLM(planner=[plan_reply(*[("jira", f"question {i}") for i in range(n)])])
    out = run_planner(llm)
    assert len(out["plan"]) == config.MAX_SUBQUESTIONS and llm.count("planner") == 1
    assert [p["id"] for p in out["plan"]] == [f"sq{i}" for i in range(1, config.MAX_SUBQUESTIONS + 1)]
    assert out["plan"][0]["question"] == "question 0"  # the first ones are kept
    assert out["warnings"] == [f"planner_truncated:{n}->{config.MAX_SUBQUESTIONS}"]


def test_repeated_sub_questions_are_dropped():
    llm = FakeLLM(planner=[plan_reply(("jira", "Same"), ("jira", "same"), ("notion", "same"))])
    assert [(p["source"], p["id"]) for p in run_planner(llm)["plan"]] == [("jira", "sq1"), ("notion", "sq2")]


def test_extra_keys_from_the_model_are_ignored():
    content = json.dumps({"sub_questions": [{"id": "a", "question": "q", "source": "jira", "reason": "r", "extra": 1}], "x": 2})
    plan, cut = validate_plan(content, NAMES)
    assert plan[0]["source"] == "jira" and cut is None


def test_long_text_is_cut_to_the_configured_limits():
    plan, _ = validate_plan(json.dumps({"sub_questions": [
        {"id": "a", "question": "w " * 500, "source": "jira", "reason": "z" * 1000}]}), NAMES)
    assert len(plan[0]["question"]) <= config.SUBQUESTION_CHARS and len(plan[0]["reason"]) <= config.REASON_CHARS


def test_llm_error_falls_back_immediately_without_a_retry():
    class Boom:
        calls = 0

        async def chat(self, *a, **k):
            Boom.calls += 1
            raise ConnectionError("down")

    out = run_planner(Boom())
    assert is_fallback(out) and Boom.calls == 1 and out["warnings"] == ["planner_fallback:llm_error:ConnectionError"]


def test_no_sources_means_no_model_call():
    llm = FakeLLM(planner=[bad()])
    out = run_planner(llm, sources=())
    assert is_fallback(out) and llm.count("planner") == 0 and out["warnings"] == ["planner_fallback:no_sources"]
