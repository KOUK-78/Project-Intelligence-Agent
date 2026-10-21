"""TOOL EXECUTOR node (act): runs the agent's calls against the MCP server. OWNER: B."""
import re
from typing import Any

from app.nodes import settings
from app.nodes.mcp_client import McpToolClient
from app.nodes.state import AgentState, compact_json

_NULLISH = {"", "null", "none", "undefined"}
_TRUE, _FALSE = {"true", "yes", "1"}, {"false", "no", "0"}


def _prop_types(prop: dict[str, Any]) -> set[str]:
    types = {prop["type"]} if isinstance(prop.get("type"), str) else set()
    for sub in prop.get("anyOf", []) or []:
        if isinstance(sub, dict) and isinstance(sub.get("type"), str):
            types.add(sub["type"])
    return types


def clean_args(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Tidy what a small model sends: drop invented args and null-ish values, coerce booleans."""
    props = (schema or {}).get("properties", {})
    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if props and key not in props:
            continue
        if value is None or (isinstance(value, str) and value.strip().lower() in _NULLISH):
            continue
        if isinstance(value, str) and "boolean" in _prop_types(props.get(key, {})):
            low = value.strip().lower()
            if low in _TRUE:
                value = True
            elif low in _FALSE:
                value = False
        cleaned[key] = value
    return cleaned


_PROJECT_WORDS = ("atlas", "project")
EMPTY_HINT = (
    "No results. Filters may be too narrow: drop label, or use keyword '*' with only status. "
    "Do not repeat this search."
)


def drop_bad_label(args: dict[str, Any]) -> dict[str, Any]:
    """Labels are single topic words. A project name or phrase there can only return nothing."""
    label = args.get("label")
    if isinstance(label, str) and (len(label.split()) > 1 or any(w in label.lower() for w in _PROJECT_WORDS)):
        return {k: v for k, v in args.items() if k != "label"}
    return args


EMPTY_WORDS_HINT = (
    "No results. Every word must match, so use 1-2 distinctive words (e.g. 'launch'), not a sentence. "
    "Do not repeat this search."
)


STATUS_WORDS = re.compile(
    r"\b(status|blocked|blocker|blockers|blocking|done|complet\w*|finished|in progress|to do|todo|open|stuck)\b", re.I
)


def guard_filters(args: dict[str, Any], question: str) -> dict[str, Any]:
    """A small model often invents filters. Keep status/label only if the question itself points at them."""
    out = dict(args)
    if "status" in out and not STATUS_WORDS.search(question or ""):
        del out["status"]
    label = out.get("label")
    if isinstance(label, str) and label.lower() not in (question or "").lower():
        del out["label"]
    return out


def normalize_call(call: dict[str, Any], schemas: dict[str, dict], question: str) -> dict[str, Any]:
    schema = schemas.get(call["name"])
    if schema is None:
        return call
    args = guard_filters(drop_bad_label(clean_args(call.get("args", {}), schema)), question)
    return {"name": call["name"], "args": args}


def empty_search_hint(name: str, args: dict[str, Any], data: Any) -> str | None:
    if name != "search_tickets" or not (isinstance(data, dict) and data.get("count") == 0):
        return None
    filtered = any(args.get(k) is not None for k in ("label", "status", "overdue"))
    return EMPTY_HINT if filtered else EMPTY_WORDS_HINT


def make_tool_node(mcp: McpToolClient):
    specs = {t["name"]: t for t in mcp.tools}

    async def tool_node(state: AgentState) -> dict[str, Any]:
        calls = state.get("pending_calls", [])
        tool_calls: list[dict] = []
        evidence: list[dict] = []
        errors: list[str] = []
        assistant_calls: list[dict] = []
        tool_messages: list[dict] = []

        for call in calls:
            name, raw_args = call["name"], call.get("args", {})
            assistant_calls.append({"function": {"name": name, "arguments": raw_args}})

            if name not in specs:
                available = ", ".join(specs)
                ok, data, latency = False, None, 0
                error = f"unknown_tool: {name!r}. Available tools: {available}"
            else:
                raw_args = normalize_call({"name": name, "args": raw_args}, {name: specs[name]["input_schema"]},
                                          state.get("question", ""))["args"]
                outcome = await mcp.call(name, raw_args)
                ok, data, error, latency = outcome.ok, outcome.data, outcome.error, outcome.latency_ms

            tool_calls.append({"name": name, "args": raw_args, "latency_ms": latency})
            evidence.append({"tool": name, "args": raw_args, "ok": ok, "data": data, "error": error})
            if error:
                errors.append(f"{name}: {error}")
            payload = data if ok else {"error": error}
            content = compact_json(payload, settings.get("MAX_TOOL_CHARS"))
            hint = empty_search_hint(name, raw_args, data) if ok else None
            if hint:
                content += "\n" + hint  # guidance for the model only; evidence stays untouched
            tool_messages.append({"role": "tool", "tool_name": name, "content": content})

        round_failed = bool(evidence) and not any(e["ok"] for e in evidence)
        consecutive = state.get("consecutive_errors", 0) + 1 if round_failed else 0
        update: dict[str, Any] = {
            "route": "agent",
            "pending_calls": [],
            "steps": state.get("steps", 0) + (0 if state.get("auto_round") else 1),
            "auto_round": False,
            "consecutive_errors": consecutive,
            "tool_calls": tool_calls,
            "evidence": evidence,
            "errors": errors,
            "messages": [{"role": "assistant", "content": "", "tool_calls": assistant_calls}, *tool_messages],
        }
        if consecutive >= 2:  # tools are failing repeatedly: stop instead of burning steps
            update.update(
                route="end",
                status="error",
                answer=f"I could not finish: the Jira tool failed repeatedly ({errors[-1] if errors else 'unknown error'}).",
            )
        return update

    return tool_node
