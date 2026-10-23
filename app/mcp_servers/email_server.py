"""Email MCP server: three READ-ONLY tools over stdio (search_emails, get_email, get_thread).

Run (the router launches it):  python -m app.mcp_servers.email_server
Data override:  EMAIL_DATA_PATH=tests/fixtures/email_stub.json

Email shape (data/SCHEMAS.md): {id, thread_id, subject, from, to, date, in_reply_to, body}.
Search snippets are the FIRST 160 CHARACTERS OF THE BODY ONLY (A's contract: never a window around the match).
RULES: stdout is the protocol, log to stderr only; no write tools; errors are objects, not crashes.
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from app.mcp_servers import _common as c

log = c.get_logger("email_server")
DEFAULT_DATA_PATH = c.REPO_ROOT / "data" / "synthetic" / "email_messages.json"
MAX_RESULTS = 10
SNIPPET_CHARS = 160
MAX_BODY_CHARS = 4000       # one message
MAX_THREAD_CHARS = 8000     # whole thread


def _load() -> list[dict[str, Any]]:
    return c.load_records(c.data_path("EMAIL_DATA_PATH", DEFAULT_DATA_PATH), "emails")


def _order(m: dict[str, Any]) -> tuple[str, str]:
    return (str(m.get("date", "")), m["id"])


def _full(m: dict[str, Any]) -> dict[str, Any]:
    body, cut = c.cap(str(m.get("body", "")), MAX_BODY_CHARS)
    out = {"id": m["id"], "thread_id": m.get("thread_id", ""), "subject": m.get("subject", ""),
           "from": m.get("from", ""), "to": m.get("to", []), "date": m.get("date", ""),
           "in_reply_to": m.get("in_reply_to"), "body": body}
    if cut:
        out["truncated"] = True
    return out


def search_emails(keyword: str) -> dict[str, Any]:
    if not isinstance(keyword, str) or not keyword.strip():
        return c.error(log, "invalid_input", "keyword must be a non-empty string; use '*' to match all emails")
    try:
        emails = _load()
    except (OSError, ValueError) as exc:
        return c.error(log, "data_unavailable", f"could not read email data: {exc}")
    toks = c.tokens(keyword)
    hits = []
    for m in emails:
        hay = " ".join([m["id"], str(m.get("subject", "")), str(m.get("from", "")), str(m.get("body", ""))]).lower()
        if all(t in hay for t in toks):
            hits.append(m)
    hits.sort(key=_order, reverse=True)
    results = [{"id": m["id"], "thread_id": m.get("thread_id", ""), "subject": m.get("subject", ""),
                "from": m.get("from", ""), "date": m.get("date", ""),
                "snippet": str(m.get("body", ""))[:SNIPPET_CHARS]} for m in hits[:MAX_RESULTS]]
    out: dict[str, Any] = {"count": len(hits), "results": results}
    if len(hits) > MAX_RESULTS:
        out["truncated"] = True
    return out


def get_email(email_id: str) -> dict[str, Any]:
    if not isinstance(email_id, str) or not email_id.strip():
        return c.error(log, "invalid_input", "email_id must be a non-empty string", id="")
    try:
        emails = _load()
    except (OSError, ValueError) as exc:
        return c.error(log, "data_unavailable", f"could not read email data: {exc}", id=email_id)
    for m in emails:
        if c.same_id(m["id"], email_id):
            return _full(m)
    return {"error": "not_found", "id": email_id}


def get_thread(thread_id: str) -> dict[str, Any]:
    """All messages of a thread, oldest first. An email ID is accepted and resolved to its thread."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        return c.error(log, "invalid_input", "thread_id must be a non-empty string", id="")
    try:
        emails = _load()
    except (OSError, ValueError) as exc:
        return c.error(log, "data_unavailable", f"could not read email data: {exc}", id=thread_id)
    wanted = thread_id.strip()
    by_email = next((m for m in emails if m["id"].upper() == wanted.upper()), None)
    if by_email is not None:
        wanted = str(by_email.get("thread_id", ""))
    msgs = sorted((m for m in emails if m.get("thread_id") == wanted and wanted), key=_order)
    if not msgs:
        return {"error": "not_found", "id": thread_id}
    budget, out_msgs, truncated = MAX_THREAD_CHARS, [], False
    for m in msgs:
        full = _full(m)
        full.pop("thread_id", None)
        if budget <= 0:
            truncated = True
            break
        body, cut = c.cap(full["body"], budget)
        full["body"] = body
        budget -= len(body)
        truncated = truncated or cut or full.get("truncated", False)
        out_msgs.append(full)
    out: dict[str, Any] = {"thread_id": wanted, "count": len(msgs), "messages": out_msgs}
    if truncated:
        out["truncated"] = True
    return out


mcp = MCPServer("email")
mcp.tool(
    name="search_emails",
    description=("Find emails by keyword across subject, sender and body ('*' = any); returns subject, "
                 "sender, date, thread and a snippet (start of the body), newest first."),
)(search_emails)
mcp.tool(
    name="get_email",
    description="Get ONE email in full by its ID.",
)(get_email)
mcp.tool(
    name="get_thread",
    description="Get every message of a conversation, oldest first, by thread ID (or any email ID in it).",
)(get_thread)


def main() -> None:
    log.info("starting email MCP server (stdio), data=%s", c.data_path("EMAIL_DATA_PATH", DEFAULT_DATA_PATH))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
