"""Notion MCP server: two READ-ONLY tools over stdio (search_pages, get_page).

Run (the router launches it):  python -m app.mcp_servers.notion_server
Data override:  NOTION_DATA_PATH=tests/fixtures/notion_stub.json

Page shape (data/SCHEMAS.md): {id, title, page_type, owner, status, created_date, last_edited, sections: [{heading, text}]}.
A page has no version field; get_page passes one through only if a record has it.
RULES: stdout is the protocol, log to stderr only; no write tools; errors are objects, not crashes.
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from app.mcp_servers import _common as c

log = c.get_logger("notion_server")
DEFAULT_DATA_PATH = c.REPO_ROOT / "data" / "synthetic" / "notion_pages.json"
MAX_RESULTS = 10
SECTION_CHARS = 400  # size of the best-matching section returned by search
MAX_PAGE_CHARS = 6000  # size cap on get_page body text


def _load() -> list[dict[str, Any]]:
    return c.load_records(c.data_path("NOTION_DATA_PATH", DEFAULT_DATA_PATH), "pages")


def _sections(page: dict[str, Any]) -> list[dict[str, str]]:
    return [s for s in page.get("sections", []) if isinstance(s, dict)]


def _sec_text(s: dict[str, Any]) -> str:
    return f"{s.get('heading', '')} {s.get('text', '')}".lower()


def search_pages(keyword: str) -> dict[str, Any]:
    """Ranked keyword search: pages with more of the query words (title hits count triple) come first, newest on ties."""
    if not isinstance(keyword, str) or not keyword.strip():
        return c.error(log, "invalid_input", "keyword must be a non-empty string; use '*' to match all pages")
    try:
        pages = _load()
    except (OSError, ValueError) as exc:
        return c.error(log, "data_unavailable", f"could not read page data: {exc}")
    terms = c.query_terms(keyword)
    scored = []
    for p in pages:
        secs = _sections(p)
        body = " ".join([p["id"], str(p.get("page_type", ""))] + [_sec_text(s) for s in secs])
        score = c.relevance(terms, str(p.get("title", "")), body)
        if score:
            scored.append((score, p, secs))
    scored.sort(key=lambda r: (r[0], str(r[1].get("last_edited", "")), r[1]["id"]), reverse=True)
    results = []
    for _, p, secs in scored[:MAX_RESULTS]:
        # The best section = the one containing the most query words (first one wins a tie).
        best = max(secs, key=lambda s: c.relevance(terms, "", _sec_text(s)), default=None)
        row = {"id": p["id"], "title": p.get("title", ""), "page_type": p.get("page_type", ""),
               "status": p.get("status", ""), "last_edited": p.get("last_edited", "")}
        if best is not None:
            text, _ = c.cap(str(best.get("text", "")), SECTION_CHARS)
            row["section"] = {"heading": best.get("heading", ""), "text": text}
        results.append(row)
    out: dict[str, Any] = {"count": len(scored), "results": results}
    if len(scored) > MAX_RESULTS:
        out["truncated"] = True
    return out


def get_page(page_id: str) -> dict[str, Any]:
    if not isinstance(page_id, str) or not page_id.strip():
        return c.error(log, "invalid_input", "page_id must be a non-empty string", id="")
    try:
        pages = _load()
    except (OSError, ValueError) as exc:
        return c.error(log, "data_unavailable", f"could not read page data: {exc}", id=page_id)
    for p in pages:
        if c.same_id(p["id"], page_id):
            budget, secs, truncated = MAX_PAGE_CHARS, [], False
            for s in _sections(p):
                text, cut = c.cap(str(s.get("text", "")), max(budget, 0))
                secs.append({"heading": s.get("heading", ""), "text": text})
                budget -= len(text)
                truncated = truncated or cut
            out = {k: p[k] for k in ("id", "title", "page_type", "owner", "status", "created_date", "last_edited", "version") if k in p}
            out["sections"] = secs
            if truncated:
                out["truncated"] = True
            return out
    return {"error": "not_found", "id": page_id}


mcp = MCPServer("notion")
mcp.tool(
    name="search_pages",
    description=("Find documentation pages (plans, decisions, specs, runbooks) by keyword across titles and "
                 "section text ('*' = any); returns status, last-edited date and the best matching section."),
)(search_pages)
mcp.tool(
    name="get_page",
    description="Get one page in full (all sections, status, last-edited date) by its ID.",
)(get_page)


def main() -> None:
    log.info("starting Notion MCP server (stdio), data=%s", c.data_path("NOTION_DATA_PATH", DEFAULT_DATA_PATH))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
