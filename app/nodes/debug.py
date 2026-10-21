"""Opt-in decision log for the agent. OWNER: B.

    PowerShell:  $env:AGENT_DEBUG="1"; python -m app ask "..." 2> debug.txt   (config.AGENT_DEBUG)

One line per graph node on stderr: step number, where the graph goes next, the tool calls the
agent asked for / that ran, and any nudge sent to the model. Off by default; never touches stdout.
"""
import sys
from typing import Any

from app import config


def enabled() -> bool:
    return config.AGENT_DEBUG


def _calls(items: list[dict]) -> list[str]:
    return [f"{c.get('name')}({c.get('args')})" for c in items or []]


def log_node(name: str, state: dict[str, Any], update: dict[str, Any]) -> None:
    if not enabled():
        return
    parts = [f"[agent-debug] {name}", f"steps={state.get('steps', 0)}", f"route={update.get('route')}"]
    if update.get("status"):
        parts.append(f"status={update['status']}")
    if update.get("pending_calls"):
        parts.append(f"asks={_calls(update['pending_calls'])}")
    if update.get("tool_calls"):
        results = [("ok" if e["ok"] else f"FAIL {e['error']}") for e in update.get("evidence", [])]
        parts.append(f"ran={_calls(update['tool_calls'])} results={results}")
    nudges = [m["content"] for m in update.get("messages", []) if m.get("role") == "user"]
    if nudges:
        parts.append(f"nudge={nudges[-1][:90]!r}")
    if update.get("errors"):
        parts.append(f"errors={update['errors']}")
    print(" | ".join(parts), file=sys.stderr, flush=True)
