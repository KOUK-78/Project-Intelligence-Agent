"""Observability: Arize Phoenix (OpenTelemetry) + a per-run JSON log (logs/runs.jsonl).

Hook points:
  * setup_tracing()      - call once at startup (runtime.py does it)
  * observe_run(q, fn)   - wraps run_agent: root span, trace_id, latency, JSONL log
  * @node_span("name")   - B decorates each graph node with this
LangChain/LangGraph model + tool calls are auto-instrumented (OpenInference).
Tracing failures NEVER break a run; the JSONL log works fully offline.
"""
import contextlib
import contextvars
import functools
import inspect
import json
import sys
import time
from datetime import datetime, timezone

from app import config

_tracer = None
_setup_done = False


def _warn(msg: str) -> None:
    print(f"[tracing] {msg}", file=sys.stderr)


def setup_tracing():
    """Register Phoenix + instrument LangChain. Safe to call repeatedly."""
    global _tracer, _setup_done
    if _setup_done:
        return _tracer
    _setup_done = True
    if not config.TRACING_ENABLED:
        return None
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register

        provider = register(
            project_name=config.PHOENIX_PROJECT_NAME,
            endpoint=config.PHOENIX_COLLECTOR_ENDPOINT,
            batch=False,  # export immediately; short CLI runs would otherwise drop spans
        )
        LangChainInstrumentor().instrument(tracer_provider=provider)
        _tracer = provider.get_tracer(__name__)
    except Exception as e:  # missing deps, Phoenix down, etc.
        _warn(f"Phoenix tracing disabled ({type(e).__name__}: {e}). JSONL log still active.")
        _tracer = None
    return _tracer


def _trace_id_hex(span) -> str | None:
    try:
        ctx = span.get_span_context()
        return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
    except Exception:
        return None


STAGE_NODES = ("planner", "retrieve", "evidence_check")  # Phase 2 nodes that get sub-question / source attributes


def _stage_attrs(name: str, state) -> dict:
    """Attributes for a Phase 2 node, read from the graph state it is about to run on. Never raises."""
    if name not in STAGE_NODES or not isinstance(state, dict):
        return {}
    attrs = {"stage.name": name}
    try:
        plan, idx = state.get("plan"), state.get("sq_index")
        if name != "planner" and isinstance(plan, list) and isinstance(idx, int) and 0 <= idx < len(plan):
            sq = plan[idx]
            attrs.update({"stage.subquestion_index": idx, "stage.subquestion_id": sq.get("id"),
                          "stage.subquestion": state.get("sq_question") or sq.get("question"),
                          "stage.source": sq.get("source"), "stage.attempt": state.get("sq_attempt")})
    except Exception:  # noqa: BLE001 - tracing never breaks a run
        pass
    return attrs


def _result_attrs(name: str, result) -> dict:
    """What the node decided, read from the update it returned. Never raises."""
    if name not in STAGE_NODES or not isinstance(result, dict):
        return {}
    attrs = {}
    try:
        if name == "planner" and isinstance(result.get("plan"), list):
            plan = result["plan"]
            attrs["stage.subquestion_count"] = len(plan)
            attrs["stage.sources"] = ",".join(str(sq.get("source")) for sq in plan)
            attrs["stage.planner_fallback"] = any(str(sq.get("reason", "")).lower().startswith("planner fallback") for sq in plan)
        if name == "evidence_check":
            if result.get("route"):
                attrs["stage.route"] = result["route"]
            added = result.get("sq_results")
            if isinstance(added, list) and added:
                attrs["stage.outcome"] = added[-1].get("status")
                attrs["stage.reason"] = added[-1].get("outcome")
        if name == "retrieve":
            attrs["stage.evidence_items"] = len(result.get("evidence") or [])
    except Exception:  # noqa: BLE001
        pass
    return attrs


def node_span(name: str):
    """Decorator for graph nodes: one span per node, tagged with the node name.

    Works on sync AND async node functions. For async nodes the span must stay open
    across the awaited work, so they get an async wrapper (a sync wrapper would just
    return the un-awaited coroutine and LangGraph would reject it).
    """
    def _tag(span):
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("node.name", name)

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                if _tracer is None:
                    return await fn(*args, **kwargs)
                with _tracer.start_as_current_span(f"node:{name}") as span:
                    _tag(span)
                    _set_attrs(span, _stage_attrs(name, args[0] if args else None))
                    result = await fn(*args, **kwargs)
                    _set_attrs(span, _result_attrs(name, result))
                    return result
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _tracer is None:
                return fn(*args, **kwargs)
            with _tracer.start_as_current_span(f"node:{name}") as span:
                _tag(span)
                _set_attrs(span, _stage_attrs(name, args[0] if args else None))
                result = fn(*args, **kwargs)
                _set_attrs(span, _result_attrs(name, result))
                return result
        return wrapper
    return deco


# ---------------------------------------------------------------- Phase 2 spans and token accounting
_usage: contextvars.ContextVar = contextvars.ContextVar("run_token_usage", default=None)


def _new_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0, "by_job": {}}


def add_usage(job: str, prompt_tokens, completion_tokens) -> None:
    """Add one model call's token counts to the current run's counter. No-op outside a run or on bad input."""
    counter = _usage.get()
    if counter is None:
        return
    p = prompt_tokens if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) else 0
    c = completion_tokens if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool) else 0
    counter["prompt_tokens"] += p
    counter["completion_tokens"] += c
    counter["total_tokens"] += p + c
    counter["calls"] += 1
    per = counter["by_job"].setdefault(job, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
    per["calls"] += 1
    per["prompt_tokens"] += p
    per["completion_tokens"] += c


def _clip(value) -> str:
    return str(value)[: config.SPAN_TEXT_CHARS]


def _set_attrs(span, attrs: dict) -> None:
    for key, value in attrs.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value if isinstance(value, (bool, int, float)) else _clip(value))
        except Exception:  # noqa: BLE001 - tracing never breaks a run
            pass


def annotate(**attrs) -> None:
    """Add attributes to the current span (for example the outcome once it is known). Safe with tracing off."""
    if _tracer is None:
        return
    try:
        from opentelemetry import trace

        _set_attrs(trace.get_current_span(), attrs)
    except Exception:  # noqa: BLE001
        pass


class _NullSpan:
    def set_attribute(self, *_a, **_k):
        pass


@contextlib.contextmanager
def stage_span(name: str, **attrs):
    """Span for a Phase 2 stage: "planner" or "evidence_check". Attributes: subquestion_index, subquestion, source...

    Yields the span (or a no-op object with tracing off) so the caller can add the outcome with
    span.set_attribute. Exceptions pass through untouched.
    """
    if _tracer is None:
        yield _NullSpan()
        return
    with _tracer.start_as_current_span(f"stage:{name}") as span:
        _set_attrs(span, {"openinference.span.kind": "CHAIN", "stage.name": name,
                          **{(k if "." in k else f"stage.{k}"): v for k, v in attrs.items()}})
        yield span


class LLMSpan:
    """Handle yielded by llm_span: report the token counts once the reply is in."""

    def __init__(self, job: str, span) -> None:
        self._job, self._span = job, span

    def set_usage(self, prompt_tokens, completion_tokens) -> None:
        add_usage(self._job, prompt_tokens, completion_tokens)
        p = prompt_tokens if isinstance(prompt_tokens, int) else 0
        c = completion_tokens if isinstance(completion_tokens, int) else 0
        _set_attrs(self._span, {"llm.token_count.prompt": p, "llm.token_count.completion": c,
                                "llm.token_count.total": p + c})


@contextlib.contextmanager
def llm_span(job: str, model: str):
    """Span around one model call. Phase 1 traces showed only node spans because the raw ollama client is not
    auto-instrumented. Token counts go on the span AND into the run's counter, even with tracing off."""
    if _tracer is None:
        yield LLMSpan(job, _NullSpan())
        return
    with _tracer.start_as_current_span(f"llm:{job}") as span:
        _set_attrs(span, {"openinference.span.kind": "LLM", "llm.model_name": model, "llm.job": job})
        yield LLMSpan(job, span)


def write_run_log(question: str, result: dict) -> None:
    """Append one JSON line per run to logs/runs.jsonl."""
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "answer": result.get("answer"),
            "citations": result.get("citations", []),
            "status": result.get("status"),
            "tool_calls": result.get("tool_calls", []),
            "latency_ms": result.get("latency_ms"),
            "trace_id": result.get("trace_id"),
        }
        for optional in ("plan", "sources", "token_usage"):  # Phase 2 fields; absent stays absent
            if result.get(optional) is not None:
                record[optional] = result[optional]
        with open(config.RUNS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        _warn(f"could not write run log: {e}")


def observe_run(question: str, fn) -> dict:
    """Run fn(question) inside a root span; fill trace_id/latency; log to JSONL."""
    t0 = time.perf_counter()
    span_cm = _tracer.start_as_current_span("run_agent") if _tracer else None
    span = span_cm.__enter__() if span_cm else None
    trace_id = None
    token = _usage.set(_new_usage())
    try:
        if span is not None:
            span.set_attribute("openinference.span.kind", "AGENT")
            span.set_attribute("input.value", question)
            trace_id = _trace_id_hex(span)
        try:
            result = fn(question)
        except Exception as e:  # contract: errors become status="error", not crashes
            result = {"answer": f"Agent error: {type(e).__name__}: {e}", "citations": [],
                      "status": "error", "tool_calls": [], "trace_id": None, "latency_ms": 0}
        result = dict(result)
        result["trace_id"] = result.get("trace_id") or trace_id
        result["latency_ms"] = result.get("latency_ms") or int((time.perf_counter() - t0) * 1000)
        counted = _usage.get()
        if result.get("token_usage") is None and counted and counted["calls"]:
            result["token_usage"] = {k: counted[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens", "calls", "by_job")}
        if span is not None and result.get("token_usage"):
            _set_attrs(span, {"llm.token_count.total": result["token_usage"].get("total_tokens")})
        if span is not None:
            span.set_attribute("output.value", str(result.get("answer", "")))
            span.set_attribute("agent.status", str(result.get("status")))
    finally:
        _usage.reset(token)
        if span_cm:
            span_cm.__exit__(None, None, None)
    write_run_log(question, result)
    return result
