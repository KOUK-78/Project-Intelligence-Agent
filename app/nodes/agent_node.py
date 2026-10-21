"""AGENT node (decide): call a tool, or declare enough evidence. OWNER: B."""
import json
import re
from typing import Any

from app import config
from app.nodes.llm import LLM, LLMReply
from app.nodes.state import AgentState
from app.nodes.tool_node import normalize_call
from app.nodes.verify import evidence_ids, mentioned_ids

AGENT_SYSTEM = (
    "You answer questions about project tickets by calling tools.\n"
    "Look facts up with tools; never answer from memory.\n"
    f"Every ticket belongs to the project {config.PROJECT_NAME}, so never use '{config.PROJECT_NAME}' or "
    "the word 'project' as a keyword or label.\n"
    "Only use status or label filters when the question itself names one.\n"
    "search_tickets: keyword '*' matches everything. status must be exactly one of "
    "To Do, In Progress, Blocked, Done. label is ONE short topic word (e.g. 'label-a'); "
    "leave label out unless you are sure of it.\n"
    "To list the tickets in one status, search keyword '*' with that status, then get_ticket for details.\n"
    "keyword is 1-2 distinctive words (e.g. 'term-a' or 'term-a term-b'), never a sentence: every word must match.\n"
    "If a search returns 0 results, use fewer or different words and fewer filters; never repeat a search.\n"
    "Search results are short summaries. Reasons, date changes and decisions are in the ticket COMMENTS, "
    "so call get_ticket on each relevant ticket before answering why/when/what-changed questions.\n"
    "You may call several tools in one reply, e.g. get_ticket for several IDs at once.\n"
    "If several tickets could answer (e.g. two tickets about the same event), read each one and compare them.\n"
    "To find what depends on a ticket, search its ID as the keyword (e.g. 'ATL-000'). "
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
    """Ticket IDs named inside tickets we read (e.g. 'Downstream tickets: ATL-000') but never read."""
    if not DEP_RE.search(state.get("question", "")):
        return []
    evidence = state.get("evidence", [])
    fetched = {e["data"].get("id") for e in evidence if e["tool"] == "get_ticket" and e["ok"] and isinstance(e["data"], dict)}
    if not fetched:
        return []
    return [i for i in mentioned_ids(evidence) if i not in fetched][: config.MAX_REF_TICKETS]


WHEN_RE = re.compile(r"\bwhen\b", re.I)


def unread_candidates(state: AgentState) -> list[str]:
    """'When' questions: tickets from a search that were never opened (up to 6). Two tickets can give
    different dates for the same event, and only reading all of them lets the answer flag the conflict."""
    if not WHEN_RE.search(state.get("question", "")):
        return []
    evidence = state.get("evidence", [])
    fetched = {e["data"].get("id") for e in evidence if e["tool"] == "get_ticket" and e["ok"] and isinstance(e["data"], dict)}
    out: list[str] = []
    for e in evidence:
        if e["tool"] == "search_tickets" and e["ok"] and isinstance(e["data"], dict):
            for row in e["data"].get("results", []):
                cid = row.get("id")
                if cid and cid not in fetched and cid not in out:
                    out.append(cid)
    return out[: config.MAX_CANDIDATE_TICKETS]


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
        return calls[: config.MAX_CALLS_PER_STEP]
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
            msg = f"llm_unavailable: {type(exc).__name__}: {exc}"[: config.ERROR_TEXT_CHARS]
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
        refs = (unread_references(state) or unread_candidates(state)) if "get_ticket" in schemas and not state.get("ref_nudged") else []
        if refs:
            # The model reads one ticket per turn, which would eat the step cap. Read the ticket IDs it
            # already saw named in one parallel round instead. System-initiated, runs once, not counted as a step.
            return {
                "route": "tool",
                "ref_nudged": True,
                "auto_round": True,
                "pending_calls": [{"name": "get_ticket", "args": {"ticket_id": i}} for i in refs],
            }
        return {"route": "synthesizer", "pending_calls": []}

    return agent_node
