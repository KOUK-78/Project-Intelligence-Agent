"""TEST-ONLY fake agent. Never used implicitly; reachable only via `--stub`.

Purpose: smoke-test the CLI, tracing and eval plumbing without Ollama or a GPU.
Its answers are fake and must never be reported as results (the eval runner writes
stub runs to a separate `*.stub.json` file for that reason).
"""
import time


def run_agent(question: str) -> dict:
    t0 = time.perf_counter()
    time.sleep(0.05)
    return {
        "answer": "Atlas is blocked by the payments API delay [ATL-112]. (STUB)",
        "citations": ["ATL-112"],
        "status": "ok",
        "trace_id": None,
        "tool_calls": [
            {"name": "search_tickets", "args": {"keyword": "Atlas", "status": "Blocked"}, "latency_ms": 12},
            {"name": "get_ticket", "args": {"ticket_id": "ATL-112"}, "latency_ms": 8},
        ],
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }
