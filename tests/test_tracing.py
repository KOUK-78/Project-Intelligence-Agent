"""C5: stage and model-call spans, token accounting, and the optional fields in the run log."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import config, tracing
from app.nodes.llm import OllamaLLM


class FakeSpan:
    def __init__(self, name):
        self.name, self.attrs = name, {}

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name):
        sp = FakeSpan(name)
        self.spans.append(sp)
        return sp


@pytest.fixture
def tracer(monkeypatch):
    t = FakeTracer()
    monkeypatch.setattr(tracing, "_tracer", t)
    return t


# ---------------------------------------------------------------- stage spans
def test_stage_span_carries_subquestion_and_source_attributes(tracer):
    with tracing.stage_span("evidence_check", subquestion_index=2, subquestion="x" * 500, source="notion") as sp:
        sp.set_attribute("stage.outcome", "insufficient")
    s = tracer.spans[0]
    assert s.name == "stage:evidence_check"
    assert s.attrs["stage.subquestion_index"] == 2 and s.attrs["stage.source"] == "notion"
    assert len(s.attrs["stage.subquestion"]) == config.SPAN_TEXT_CHARS          # long text is clipped
    assert s.attrs["stage.name"] == "evidence_check" and s.attrs["stage.outcome"] == "insufficient"


def test_planner_span_and_none_attributes_are_skipped(tracer):
    with tracing.stage_span("planner", subquestion_count=3, note=None):
        pass
    assert tracer.spans[0].name == "stage:planner"
    assert "stage.note" not in tracer.spans[0].attrs and tracer.spans[0].attrs["stage.subquestion_count"] == 3


def test_stage_span_lets_exceptions_through(tracer):
    with pytest.raises(KeyError):
        with tracing.stage_span("planner"):
            raise KeyError("x")


def test_everything_is_a_no_op_with_tracing_off(monkeypatch):
    monkeypatch.setattr(tracing, "_tracer", None)
    with tracing.stage_span("planner", a=1) as sp:
        sp.set_attribute("k", "v")
    with tracing.llm_span("agent", "m") as sp:
        sp.set_usage(5, 6)
    tracing.annotate(x=1)


# ---------------------------------------------------------------- token counting
def test_llm_span_records_counts_on_the_span_and_in_the_counter(tracer):
    tok = tracing._usage.set(tracing._new_usage())
    try:
        with tracing.llm_span("planner", "qwen3:8b") as sp:
            sp.set_usage(100, 20)
        with tracing.llm_span("agent", "qwen3:8b") as sp:
            sp.set_usage(50, 5)
        c = tracing._usage.get()
    finally:
        tracing._usage.reset(tok)
    a = tracer.spans[0].attrs
    assert tracer.spans[0].name == "llm:planner" and a["llm.model_name"] == "qwen3:8b"
    assert (a["llm.token_count.prompt"], a["llm.token_count.completion"], a["llm.token_count.total"]) == (100, 20, 120)
    assert c["total_tokens"] == 175 and c["calls"] == 2
    assert c["by_job"]["planner"] == {"prompt_tokens": 100, "completion_tokens": 20, "calls": 1}


def test_counts_still_accumulate_with_tracing_off(monkeypatch):
    monkeypatch.setattr(tracing, "_tracer", None)
    tok = tracing._usage.set(tracing._new_usage())
    try:
        with tracing.llm_span("agent", "m") as sp:
            sp.set_usage(10, 2)
        assert tracing._usage.get()["total_tokens"] == 12
    finally:
        tracing._usage.reset(tok)


def test_bad_counts_are_treated_as_zero_and_outside_a_run_nothing_breaks():
    tracing.add_usage("agent", 5, 5)                      # no counter active: no-op
    tok = tracing._usage.set(tracing._new_usage())
    try:
        tracing.add_usage("agent", None, "x")
        tracing.add_usage("agent", True, 3)
        assert tracing._usage.get()["total_tokens"] == 3 and tracing._usage.get()["calls"] == 2
    finally:
        tracing._usage.reset(tok)


# ---------------------------------------------------------------- observe_run
def test_observe_run_attaches_token_usage_and_scopes_the_counter(monkeypatch, tmp_path):
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")

    def agent(q):
        tracing.add_usage("agent", 40, 10)
        tracing.add_usage("synthesizer", 30, 5)
        return {"answer": "a", "citations": [], "status": "ok", "trace_id": None, "tool_calls": [], "latency_ms": 7}

    r = tracing.observe_run("q", agent)
    assert r["token_usage"]["total_tokens"] == 85 and r["token_usage"]["by_job"]["synthesizer"]["prompt_tokens"] == 30
    assert tracing._usage.get() is None                      # counter does not leak into the next run
    again = tracing.observe_run("q", lambda q: {"answer": "a", "citations": [], "status": "ok", "trace_id": None,
                                                "tool_calls": [], "latency_ms": 1})
    assert "token_usage" not in again                        # nothing counted: the field stays absent
    logged = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert logged[0]["token_usage"]["total_tokens"] == 85 and "token_usage" not in logged[1]


def test_an_agents_own_token_usage_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")

    def agent(q):
        tracing.add_usage("agent", 1, 1)
        return {"answer": "a", "citations": [], "status": "ok", "trace_id": None, "tool_calls": [], "latency_ms": 1,
                "token_usage": {"total_tokens": 999}}

    assert tracing.observe_run("q", agent)["token_usage"] == {"total_tokens": 999}


def test_run_log_keeps_plan_and_sources_only_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")
    base = {"answer": "a", "citations": [], "status": "ok", "tool_calls": [], "latency_ms": 1, "trace_id": "t"}
    tracing.write_run_log("q1", base)
    tracing.write_run_log("q2", {**base, "plan": {"valid": True}, "sources": {"jira": "ok"}})
    one, two = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert not {"plan", "sources", "token_usage"} & set(one)
    assert two["plan"] == {"valid": True} and two["sources"] == {"jira": "ok"}


# ---------------------------------------------------------------- llm.py reports usage
def test_ollama_llm_reports_token_counts_from_the_response(monkeypatch, tracer):
    reply = SimpleNamespace(message=SimpleNamespace(content="<think>x</think>hi", tool_calls=None),
                            prompt_eval_count=321, eval_count=45)

    class Client:
        def __init__(self, **kw): pass
        async def chat(self, **kw): return reply

    monkeypatch.setitem(__import__("sys").modules, "ollama", SimpleNamespace(AsyncClient=Client))
    tok = tracing._usage.set(tracing._new_usage())
    try:
        out = asyncio.run(OllamaLLM().chat("planner", [{"role": "user", "content": "q"}]))
        used = tracing._usage.get()
    finally:
        tracing._usage.reset(tok)
    assert out.content == "hi"
    assert used["by_job"]["planner"] == {"prompt_tokens": 321, "completion_tokens": 45, "calls": 1}
    assert tracer.spans[0].name == "llm:planner" and tracer.spans[0].attrs["llm.token_count.total"] == 366


def test_a_response_without_counts_does_not_break_the_call(monkeypatch, tracer):
    reply = SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))

    class Client:
        def __init__(self, **kw): pass
        async def chat(self, **kw): return reply

    monkeypatch.setitem(__import__("sys").modules, "ollama", SimpleNamespace(AsyncClient=Client))
    assert asyncio.run(OllamaLLM().chat("agent", [])).content == "ok"
