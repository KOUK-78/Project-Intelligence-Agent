"""Glue: pick real or stub run_agent, start tracing, wrap with observe_run."""
import sys

from app import tracing


def get_run_agent(use_stub: bool = False):
    """Return a callable question -> AgentResult (traced + logged)."""
    if use_stub:
        from app.stub_agent import run_agent as raw
    else:
        try:
            from app.agent import run_agent as raw
        except ImportError:
            print("[runtime] app.agent not available yet; falling back to STUB.", file=sys.stderr)
            from app.stub_agent import run_agent as raw
    tracing.setup_tracing()
    return lambda question: tracing.observe_run(question, raw)
