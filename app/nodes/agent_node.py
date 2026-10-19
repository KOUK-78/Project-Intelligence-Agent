"""AGENT node (decide): call a tool, or declare enough evidence. OWNER: B."""
import json
from typing import Any

from app import config
from app.nodes import settings
from app.nodes.llm import LLM, LLMReply
from app.nodes.state import AgentState

AGENT_SYSTEM = (
    "You answer questions about project tickets by calling tools.\n"
    "Look facts up with tools; never answer from memory.\n"
    "Search by keyword or label, and use get_ticket for details and comments.\n"
    f"Today's date is {config.TODAY}.\n"
    "When you have enough evidence, stop calling tools and reply with the single word DONE."
)

NUDGE = "You have no evidence yet. Call a tool to look this up."


def loop_cap_message() -> str:
    return (
        f"I could not finish: I reached the limit of {config.MAX_AGENT_STEPS} tool steps "
        "without having enough evidence to answer."
    )


def to_ollama_tools(tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"] or {"type": "object", "properties": {}},
            },
        }
        for t in tool_specs
    ]


def _as_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_json_object(text: str) -> dict[str, Any] | None:
    """First {...} object in text (tolerates prose, code fences, <tool_call> tags)."""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except ValueError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None


def extract_calls(reply: LLMReply) -> list[dict[str, Any]]:
    """Native tool calls first; fall back to {"tool": ..., "args": {...}} JSON in the text."""
    calls = [
        {"name": tc["name"], "args": _as_args(tc.get("arguments"))}
        for tc in reply.tool_calls
        if tc.get("name")
    ]
    if calls:
        return calls[: settings.get("MAX_CALLS_PER_STEP")]
    obj = _first_json_object(reply.content)
    if obj:
        name = obj.get("tool") or obj.get("name")
        if isinstance(name, str) and name:
            args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
            return [{"name": name, "args": _as_args(args)}]
    return []


def make_agent_node(llm: LLM, tool_specs: list[dict[str, Any]]):
    tools = to_ollama_tools(tool_specs)

    async def agent_node(state: AgentState) -> dict[str, Any]:
        messages = [{"role": "system", "content": AGENT_SYSTEM}, *state.get("messages", [])]
        try:
            reply = await llm.chat("agent", messages, tools=tools)
        except Exception as exc:  # noqa: BLE001 - Ollama down, timeout, bad model name...
            msg = f"llm_unavailable: {type(exc).__name__}: {exc}"[:300]
            return {
                "route": "end",
                "status": "error",
                "answer": f"I could not reach the language model. ({msg})",
                "errors": [msg],
            }

        calls = extract_calls(reply)
        if calls:
            if state.get("steps", 0) >= config.MAX_AGENT_STEPS:
                return {
                    "route": "end",
                    "status": "loop_cap",
                    "answer": loop_cap_message(),
                    "pending_calls": [],
                }
            return {"route": "tool", "pending_calls": calls}

        # No tool call = the agent says it is done. If it has looked at nothing yet, nudge once.
        if not state.get("evidence") and not state.get("nudged"):
            return {
                "route": "agent",
                "nudged": True,
                "pending_calls": [],
                "messages": [
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": NUDGE},
                ],
            }
        return {"route": "synthesizer", "pending_calls": []}

    return agent_node
