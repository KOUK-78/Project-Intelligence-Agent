"""Source registry: one entry per data source the agent can reach. OWNER: D.

A Source says how a source is reached (server module, data path variable) and how its
evidence is recognised (ID prefix, tool names). Everything that used to hardcode Jira
(launching the server, the ticket ID check, the argument guards) reads this registry instead.

Phase 2 starts with ONLY Jira registered so Phase 1 behaviour is reproduced exactly.
Notion and email are added by their own tasks (D3, D4) with one `register(Source(...))` each.
Rules: no IDs, titles or data wording in this file (prompt-hygiene tests apply).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import config

# (args, question) -> args. Runs after the generic clean-up, only for this source's tools.
Normalizer = Callable[[dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class Source:
    name: str                        # short key used in plans and logs
    server_module: str               # launched as `python -m <module>` from the repo root
    id_prefix: str                   # citation prefix, e.g. "ATL" -> ATL-000
    data_env: str                    # env var that overrides the data path for the server
    data_path: Path                  # default data file
    tool_names: tuple[str, ...]      # tools this server advertises
    records_key: str | None = None   # top-level key holding the records, None = file is a list
    id_field: str = "id"
    description: str = ""            # one generic line for the planner: what kind of records live here
    normalizer: Normalizer | None = None

    @property
    def id_re(self) -> re.Pattern[str]:
        return re.compile(rf"\b{re.escape(self.id_prefix)}-\d+\b")

    def resolved_data_path(self, env: dict[str, str] | None = None) -> Path:
        override = (env if env is not None else os.environ).get(self.data_env)
        return Path(override) if override else self.data_path


def _jira_normalizer(args: dict[str, Any], question: str) -> dict[str, Any]:
    """Jira-only guards (label and status). Imported lazily: tool_node imports this module."""
    from app.nodes.tool_node import jira_guards

    return jira_guards(args, question)


JIRA = Source(
    name="jira",
    server_module="app.mcp_servers.jira_server",
    id_prefix="ATL",
    data_env="JIRA_DATA_PATH",
    data_path=config.DATA_PATH,
    tool_names=("search_tickets", "get_ticket", "list_overdue"),
    records_key="tickets",
    description="work tickets: status, assignee, due dates, priority and comments",
    normalizer=_jira_normalizer,
)

NOTION = Source(
    name="notion",
    server_module="app.mcp_servers.notion_server",
    id_prefix="NTN",
    data_env="NOTION_DATA_PATH",
    data_path=config.ROOT / "data" / "synthetic" / "notion_pages.json",
    tool_names=("search_pages", "get_page"),
    records_key=None,
    description="documentation pages: plans, decisions, specs, runbooks and process notes",
)

EMAIL = Source(
    name="email",
    server_module="app.mcp_servers.email_server",
    id_prefix="EML",
    data_env="EMAIL_DATA_PATH",
    data_path=config.ROOT / "data" / "synthetic" / "email_messages.json",
    tool_names=("search_emails", "get_email", "get_thread"),
    records_key=None,
    description="email messages and threads with people inside and outside the team",
)

_REGISTRY: dict[str, Source] = {}


def register(source: Source) -> Source:
    """Add a source. Duplicate names, prefixes or tool names are rejected at registration."""
    if source.name in _REGISTRY:
        raise ValueError(f"source {source.name!r} already registered")
    for other in _REGISTRY.values():
        if other.id_prefix == source.id_prefix:
            raise ValueError(f"ID prefix {source.id_prefix!r} already used by {other.name!r}")
        clash = set(other.tool_names) & set(source.tool_names)
        if clash:
            raise ValueError(f"tool names {sorted(clash)} already provided by {other.name!r}")
    _REGISTRY[source.name] = source
    return source


def all_sources() -> list[Source]:
    return list(_REGISTRY.values())


def get_source(name: str) -> Source | None:
    return _REGISTRY.get(name)


def source_for_tool(tool: str) -> Source | None:
    return next((s for s in _REGISTRY.values() if tool in s.tool_names), None)


def source_for_id(item_id: str) -> Source | None:
    prefix = item_id.split("-", 1)[0]
    return next((s for s in _REGISTRY.values() if s.id_prefix == prefix), None)


def normalize_for_tool(tool: str, args: dict[str, Any], question: str) -> dict[str, Any]:
    """Run the owning source's normalizer, if it has one."""
    src = source_for_tool(tool)
    return src.normalizer(args, question) if src and src.normalizer else args


_CATALOG = {"jira": JIRA, "notion": NOTION, "email": EMAIL}


def configure_sources(names: tuple[str, ...] | list[str]) -> list[Source]:
    """Make the registry hold exactly `names`, in catalog order (the ENABLED_SOURCES switch)."""
    unknown = [n for n in names if n not in _CATALOG]
    if unknown or not names:
        raise ValueError(f"unknown or empty source list {list(names)}; known sources: {list(_CATALOG)}")
    _REGISTRY.clear()
    for name in _CATALOG:
        if name in names:
            register(_CATALOG[name])
    return all_sources()


def enable_phase2_sources() -> list[Source]:
    """Register Notion and email (idempotent). Called when the planner is on; with it off only Jira
    is registered, which reproduces Phase 1 exactly."""
    for src in (NOTION, EMAIL):
        if src.name not in _REGISTRY:
            register(src)
    return all_sources()


def scope_sources() -> list:
    """The registry as B's planner/scope records (app.nodes.scope.Source): name, description, tools."""
    from app.nodes.scope import Source as ScopeSource

    return [ScopeSource(name=s.name, description=s.description, tools=s.tool_names) for s in all_sources()]


def phase2_runtime() -> tuple[Any, list | None]:
    """(mcp client, planner sources) for arun_agent. Which sources a run reaches is config.ENABLED_SOURCES:

      planner off, jira only (the default)  -> (None, None): the default single-server client, Phase 1 untouched
      planner off, several sources          -> (router, None): FLAT MODE, one loop over all their tools (M2 baseline)
      planner on                            -> (router, planner sources)

    The registry is set to the enabled sources first, so known IDs (the union over registered sources) match."""
    planner = config.PLANNER_ENABLED
    names = config.resolve_enabled_sources(planner=planner)
    configure_sources(names)
    if names == ("jira",) and not planner:
        return None, None
    from app.nodes.mcp_client import McpRouter

    return McpRouter(), (scope_sources() if planner else None)


def load_ids(source: Source, env: dict[str, str] | None = None) -> set[str] | None:
    """Every record ID in the source's data file, or None if the file can't be read."""
    import json

    try:
        raw = json.loads(source.resolved_data_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = raw.get(source.records_key, []) if isinstance(raw, dict) and source.records_key else raw
    if not isinstance(items, list):
        return None
    ids = {r[source.id_field] for r in items if isinstance(r, dict) and isinstance(r.get(source.id_field), str)}
    return ids or None


register(JIRA)
