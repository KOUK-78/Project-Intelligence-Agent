"""Jira MCP server: three READ-ONLY tools over stdio.

Run (the agent launches this as a subprocess):  python -m app.mcp_servers.jira_server

RULES
- NEVER write to stdout: it carries the MCP protocol. Log to stderr only.
- No write actions. Tools only read the JSON file; nothing is ever modified.
- Bad input returns a clean {"error": ...} object, never an exception.

Env override for building against the 3-ticket stub:
  JIRA_DATA_PATH=data/synthetic/jira_tickets_stub.json
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s jira_server: %(message)s",
)
log = logging.getLogger("jira_server")

REPO_ROOT = Path(__file__).resolve().parents[2]

# Local fallbacks until app/config.py (owned by C) is importable. When it is,
# its values win, so TODAY has exactly one source of truth.
TODAY = "2026-06-30"
DEFAULT_DATA_PATH = REPO_ROOT / "data" / "synthetic" / "jira_tickets.json"
DATA_PATH: str | Path = DEFAULT_DATA_PATH
try:
    from app import config as _cfg  # type: ignore

    TODAY = _cfg.TODAY
    DATA_PATH = _cfg.DATA_PATH
except Exception as exc:  # config missing or not importable: use the constants above
    log.info("app.config not used (%s); falling back to local constants", type(exc).__name__)

VALID_STATUSES = ["To Do", "In Progress", "Blocked", "Done"]
MAX_RESULTS = 15
# Words ignored in keyword search. Every ticket is Atlas, so those words carry no signal.
_IGNORED = {"the", "a", "an", "of", "is", "are", "what", "which", "why", "for",
            "to", "in", "on", "and", "or", "atlas", "project"}


# ----------------------------------------------------------------- helpers
def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"error": code, **extra, "message": message}
    log.warning("returning error: %s", out)
    return out


def _data_path() -> Path:
    env = os.environ.get("JIRA_DATA_PATH")
    p = Path(env or DATA_PATH)
    p = p if p.is_absolute() else REPO_ROOT / p
    if not env and not p.exists() and DEFAULT_DATA_PATH.exists():
        log.warning("DATA_PATH %s not found; using %s (fix config.DATA_PATH)", p, DEFAULT_DATA_PATH)
        return DEFAULT_DATA_PATH
    return p


def _load() -> list[dict[str, Any]]:
    # Re-read on every call: the file is tiny, and it avoids stale state.
    with open(_data_path(), encoding="utf-8") as f:
        return json.load(f)


def _today() -> date:
    return date.fromisoformat(TODAY)


def _days_overdue(t: dict[str, Any]) -> int:
    """Days past due, or 0 if not overdue. Overdue = due_date < TODAY and status != Done."""
    if t["status"] == "Done":
        return 0
    return max((_today() - date.fromisoformat(t["due_date"])).days, 0)


def _summary(t: dict[str, Any]) -> dict[str, Any]:
    return {k: t[k] for k in ("id", "title", "status", "assignee", "due_date", "priority")}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", s.strip().lower())


# ------------------------------------------------------------------- tools
def search_tickets(
    keyword: str,
    status: Optional[str] = None,
    label: Optional[str] = None,
    overdue: Optional[bool] = None,
) -> dict[str, Any]:
    if not isinstance(keyword, str) or not keyword.strip():
        return _error("invalid_input", "keyword must be a non-empty string; use '*' to match all tickets")

    status_filter = None
    if status is not None and _norm(str(status)) not in ("", "all", "any", "none", "null"):
        canon = {_norm(s): s for s in VALID_STATUSES}
        status_filter = canon.get(_norm(str(status)))
        if status_filter is None:
            return _error("invalid_input", f"status must be one of {VALID_STATUSES}", valid=VALID_STATUSES)

    label_filter = None
    if label is not None and _norm(str(label)) not in ("", "all", "any", "none", "null"):
        label_filter = _norm(str(label))

    try:
        tickets = _load()
    except (OSError, ValueError) as exc:
        return _error("data_unavailable", f"could not read ticket data: {exc}")

    # '*' (or only ignorable words) means no keyword restriction; otherwise ALL tokens must match.
    tokens = [] if keyword.strip() == "*" else [
        w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]*", keyword.lower()) if w not in _IGNORED
    ]

    matches = []
    for t in tickets:
        if status_filter and t["status"] != status_filter:
            continue
        if label_filter and label_filter not in {_norm(x) for x in t["labels"]}:
            continue
        if overdue is not None and (_days_overdue(t) > 0) != bool(overdue):
            continue
        hay = " ".join(
            [t["id"], t["title"], t["description"], " ".join(t["labels"]), t["assignee"]]
            + [c["text"] for c in t["comments"]]
        ).lower()
        if all(tok in hay for tok in tokens):
            matches.append(t)

    matches.sort(key=lambda t: (t["due_date"], t["id"]))
    out: dict[str, Any] = {
        "count": len(matches),
        "results": [_summary(t) for t in matches[:MAX_RESULTS]],
    }
    if len(matches) > MAX_RESULTS:
        out["truncated"] = True  # only present when results were cut; narrow the search
    return out


def get_ticket(ticket_id: str) -> dict[str, Any]:
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        return _error("invalid_input", "ticket_id must be a non-empty string like 'ATL-112'", id="")
    raw = ticket_id.strip()
    key = raw.upper()
    if key.isdigit():  # tolerate "112"
        key = f"ATL-{key}"
    try:
        tickets = _load()
    except (OSError, ValueError) as exc:
        return _error("data_unavailable", f"could not read ticket data: {exc}", id=raw)
    for t in tickets:
        if t["id"] == key:
            return copy.deepcopy(t)
    return {"error": "not_found", "id": raw}


def list_overdue() -> dict[str, Any]:
    try:
        tickets = _load()
    except (OSError, ValueError) as exc:
        return _error("data_unavailable", f"could not read ticket data: {exc}")
    rows = []
    for t in tickets:
        d = _days_overdue(t)
        if d > 0:
            rows.append({
                "id": t["id"], "title": t["title"], "status": t["status"],
                "assignee": t["assignee"], "due_date": t["due_date"], "days_overdue": d,
            })
    rows.sort(key=lambda r: (-r["days_overdue"], r["id"]))
    return {"count": len(rows), "results": rows}


# ------------------------------------------------------------ registration
mcp = MCPServer("jira")

# One short, distinct line each: the model reads these to pick a tool.
mcp.tool(
    name="search_tickets",
    description=(
        "Find tickets by keyword across title, description, labels and comments ('*' = any), "
        "optionally filtered by status, label or overdue; returns short summaries."
    ),
)(search_tickets)
mcp.tool(
    name="get_ticket",
    description="Get the full details and comments of ONE ticket by its ID (e.g. ATL-112).",
)(get_ticket)
mcp.tool(
    name="list_overdue",
    description="List every ticket past its due date and not Done, with days overdue.",
)(list_overdue)


def main() -> None:
    log.info("starting Jira MCP server (stdio), TODAY=%s, data=%s", TODAY, _data_path())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
