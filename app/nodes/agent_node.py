"""AGENT node (decide): call a tool, or declare enough evidence. OWNER: B."""
import json
import re
from typing import Any

from app import config
from app.nodes import settings
from app.nodes.llm import LLM, LLMReply
from app.nodes.state import AgentState
from app.nodes.tool_node import normalize_call
from app.nodes.verify import evidence_ids, mentioned_ids

AGENT_SYSTEM = (
    "You answer questions about project tickets by calling tools.\n"
    "Look facts up with tools; never answer from memory.\n"
    "Every ticket is in Project Atlas, so never use 'Atlas' or 'Project Atlas' as a keyword or label.\n"
    "Only use status or label filters when the question itself names one.\n"
    "search_tickets: keyword '*' matches everything. status must be exactly one of "
    "To Do, In Progress, Blocked, Done. label is ONE short topic word (e.g. payments); "
    "leave label out unless you are sure of it.\n"
    "For 'what is blocked/blocking', search keyword '*' with status 'Blocked', then get_ticket for details.\n"
    "keyword is 1-2 distinctive words (e.g. 'launch', 'privacy policy'), never a sentence: every word must match.\n"
    "If a search returns 0 results, use fewer or different words and fewer filters; never repeat a search.\n"
    "Search results are short summaries. Reasons, date changes and decisions are in the ticket COMMENTS, "
    "so call get_ticket on each relevant ticket before answering why/when/what-changed questions.\n"
    "To find what depends on a ticket, search its ID as the keyword (e.g. 'ATL-112'). "
    "If a ticket says it waits on another ticket, look that one up too.\n"
    f"Today's date is {config.TODAY}.\n"
    "When you have enough evidence, stop calling tools and reply with the single word DONE."
)

NUDGE = "You have no evidence yet. Call a tool to look this up."
DETAIL_NUDGE = (
    "You only have search summaries. Call get_ticket on the relevant tickets: "
    "their descriptions and comments hold the reasons."
)
# Question types whose answer usually lives in a ticket's comments, not its summary line.
DETAIL_RE = re.compile(r"\b(why|because|reason|chang\w*|moved|slipp\w*|late|delay\w*|blocking|depend\w*|caused?)\b", re.I)


# Dependency questions: the answer spans the tickets that a blocker names, so read those too.
DEP_RE = re.compile(r"\b(block\w*|depend\w*|waiting|holds? up|held up|affect\w*)\b", re.I)


def unread_references(state: AgentState) -> list[str]:
    """Ticket IDs named inside tickets we read (e.g. 'Downstream tickets: ATL-118') but never read."""
    if not DEP_RE.search(state.get("question", "")):
        return []
    evidence = state.get("evidence", [])
    fetched = {e["data"].get("id") for e in evidence if e["tool"] == "get_ticket" and e["ok"] and isinstance(e["data"], dict)}
    if not fetched:
        return []
    return [i for i in mentioned_ids(evidence) if i not in fetched][:4]


def needs_ticket_details(state: AgentState) -> bool:
    evidence = state.get("evidence", [])
    saw_tickets = bool(evidence_ids(evidence))
    fetched = any(e["tool"] == "get_ticket" and e["ok"] for e in evidence)
    return saw_tickets and not fetched and bool(DETAIL_RE.search(state.get("question", "")))


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
    schemas = {t["name"]: t["input_schema"] for t in tool_specs}

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
            # Same search again adds nothing and burns a step: drop repeats of calls that already succeeded.
            seen = {(e["tool"], json.dumps(e["args"], sort_keys=True, default=str))
                    for e in state.get("evidence", []) if e["ok"]}
            fresh = []
            for call in (normalize_call(c, schemas, state.get("question", "")) for c in calls):
                key = (call["name"], json.dumps(call["args"], sort_keys=True, default=str))
                if key not in seen:
                    seen.add(key)
                    fresh.append(call)
            calls = fresh  # all repeats -> no new calls -> treated as "done" below
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
        can_continue = state.get("steps", 0) < config.MAX_AGENT_STEPS
        if can_continue and needs_ticket_details(state) and not state.get("detail_nudged"):
            return {
                "route": "agent",
                "detail_nudged": True,
                "pending_calls": [],
                "messages": [
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": DETAIL_NUDGE},
                ],
            }
        refs = unread_references(state) if can_continue and not state.get("ref_nudged") else []
        if refs:
            return {
                "route": "agent",
                "ref_nudged": True,
                "pending_calls": [],
                "messages": [
                    {"role": "assistant", "content": reply.content},
                    {"role": "user", "content": "These tickets are named in what you read but you have not read them yet: "
                     + ", ".join(refs) + ". Call get_ticket on them before answering."},
                ],
            }
        return {"route": "synthesizer", "pending_calls": []}

    return agent_node
