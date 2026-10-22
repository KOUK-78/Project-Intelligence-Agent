"""Glue: pick the agent, start tracing, wrap with observe_run.

The stub is used ONLY when asked for (--stub). There is no silent fallback: if the real
agent cannot be imported, we stop with a clear error so a broken agent is never measured
as if it were working.
"""
from app import tracing


def get_run_agent(use_stub: bool = False):
    """Return a callable question -> AgentResult (traced + logged)."""
    if use_stub:
        from app.stub_agent import run_agent as raw  # TEST-ONLY, explicit opt-in
    else:
        try:
            from app.agent import run_agent as raw
        except ImportError as e:
            raise RuntimeError(
                f"Cannot import the real agent (app.agent.run_agent): {e}. "
                "Fix the import (missing dependency? run `pip install -r requirements.txt`). "
                "Use --stub only for a smoke test; it is never a substitute."
            ) from e
    tracing.setup_tracing()
    return lambda question: tracing.observe_run(question, raw)
