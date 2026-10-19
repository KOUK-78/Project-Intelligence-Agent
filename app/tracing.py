"""Observability: Arize Phoenix (OpenTelemetry) + a per-run JSON log (logs/runs.jsonl).

Hook points:
  * setup_tracing()      - call once at startup (runtime.py does it)
  * observe_run(q, fn)   - wraps run_agent: root span, trace_id, latency, JSONL log
  * @node_span("name")   - B decorates each graph node with this
LangChain/LangGraph model + tool calls are auto-instrumented (OpenInference).
Tracing failures NEVER break a run; the JSONL log works fully offline.
"""
import functools
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


def node_span(name: str):
    """Decorator for graph nodes: one span per node, tagged with the node name."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _tracer is None:
                return fn(*args, **kwargs)
            with _tracer.start_as_current_span(f"node:{name}") as span:
                span.set_attribute("openinference.span.kind", "CHAIN")
                span.set_attribute("node.name", name)
                return fn(*args, **kwargs)
        return wrapper
    return deco


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
        if span is not None:
            span.set_attribute("output.value", str(result.get("answer", "")))
            span.set_attribute("agent.status", str(result.get("status")))
    finally:
        if span_cm:
            span_cm.__exit__(None, None, None)
    write_run_log(question, result)
    return result
