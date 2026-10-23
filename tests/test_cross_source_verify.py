"""Verifier and tool-node generalisation (D5). Plain Python, no model."""
import asyncio
import json
from pathlib import Path

import pytest

from app import sources
from app.nodes import verify as v
from app.nodes.tool_node import make_tool_node

FIX = Path(__file__).parent / "fixtures"
SYN = Path(__file__).parents[1] / "data" / "synthetic"


@pytest.fixture
def three_sources(monkeypatch):
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))
    sources.enable_phase2_sources()
    monkeypatch.setenv("JIRA_DATA_PATH", str(FIX / "jira_stub.json"))
    monkeypatch.setenv("NOTION_DATA_PATH", str(SYN / "notion_pages_stub.json"))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(SYN / "email_messages_stub.json"))


def ev(tool, data, ok=True):
    return {"tool": tool, "args": {}, "ok": ok, "data": data, "error": None}


def test_default_registry_is_jira_only():
    assert [s.name for s in sources.all_sources()] == ["jira"]


def test_enable_phase2_sources_is_idempotent(three_sources):
    sources.enable_phase2_sources()
    assert [s.name for s in sources.all_sources()] == ["jira", "notion", "email"]


def test_known_ids_span_all_sources(three_sources):
    ids = v.load_known_ids()
    assert {"ATL-112", "NTN-104", "EML-144"} <= ids
    assert v.unreadable_prefixes() == set()


def test_known_ids_with_explicit_path_is_phase1_behaviour(three_sources):
    ids = v.load_known_ids(FIX / "jira_stub.json")
    assert "ATL-112" in ids and not any(i.startswith(("NTN", "EML")) for i in ids)


def test_ntn_and_eml_citations_verified(three_sources):
    evidence = [ev("get_page", {"id": "NTN-104", "title": "x"}), ev("get_email", {"id": "EML-101", "body": "y"})]
    answer, cites, warns = v.verify_citations(
        "The design is in [NTN-104] and the delay in [EML-101].", [],
        known_ids=v.load_known_ids(), ev_ids=v.evidence_ids(evidence))
    assert cites == ["NTN-104", "EML-101"] and warns == []


def test_unknown_prefix_is_rejected(three_sources):
    answer, cites, warns = v.verify_citations(
        "See [XYZ-001].", [], known_ids=v.load_known_ids(), ev_ids=set())
    assert cites == [] and "unknown_ticket_id:XYZ-001" in warns and "[unverified: XYZ-001]" in answer


def test_known_prefix_but_unknown_number_is_rejected(three_sources):
    _, cites, warns = v.verify_citations("See [NTN-999].", [], known_ids=v.load_known_ids(), ev_ids={"NTN-999"})
    assert cites == [] and "unknown_ticket_id:NTN-999" in warns


def test_known_id_not_retrieved_is_not_in_evidence(three_sources):
    _, cites, warns = v.verify_citations("See [EML-111].", [], known_ids=v.load_known_ids(), ev_ids=set())
    assert cites == [] and "not_in_evidence:EML-111" in warns


def test_email_mentioning_a_ticket_allows_a_mention_citation(three_sources):
    evidence = [ev("get_email", {"id": "EML-101", "body": "See ATL-112 for status."})]
    assert v.mentioned_ids(evidence) == ["ATL-112"]
    _, cites, warns = v.verify_citations(
        "Status is tracked in [ATL-112], per [EML-101].", [], known_ids=v.load_known_ids(),
        ev_ids=v.evidence_ids(evidence), mentioned=set(v.mentioned_ids(evidence)))
    assert cites == ["ATL-112", "EML-101"] and "cited_from_mention:ATL-112" in warns


def test_unreadable_source_checked_against_evidence_only(three_sources, monkeypatch):
    monkeypatch.setenv("NOTION_DATA_PATH", "/nonexistent.json")
    assert v.unreadable_prefixes() == {"NTN"}
    ids, prefixes = v.load_known_ids(), v.unreadable_prefixes()
    assert not any(i.startswith("NTN") for i in ids)
    ok = v.verify_citations("See [NTN-104].", [], known_ids=ids, ev_ids={"NTN-104"}, unchecked_prefixes=prefixes)
    assert ok[1] == ["NTN-104"]
    unretrieved = v.verify_citations("See [NTN-105].", [], known_ids=ids, ev_ids=set(), unchecked_prefixes=prefixes)
    assert unretrieved[1] == [] and "not_in_evidence:NTN-105" in unretrieved[2]


def test_tool_node_failure_text_is_source_neutral():
    class Dead:
        tools = [{"name": "get_page", "description": "", "input_schema": {"type": "object", "properties": {}}}]

        async def call(self, name, args):
            from app.nodes.mcp_client import ToolOutcome
            return ToolOutcome(False, error="source_unavailable: notion: down")

    node = make_tool_node(Dead())
    state = {"pending_calls": [{"name": "get_page", "args": {}}], "consecutive_errors": 1, "question": "q"}
    out = asyncio.run(node(state))
    assert out["status"] == "error" and "Jira" not in out["answer"] and "source_unavailable" in out["answer"]


def test_thread_ids_are_not_mentions_or_citations(three_sources):
    evidence = [ev("get_email", {"id": "EML-101", "thread_id": "THR-201", "body": "see ATL-112"})]
    assert v.mentioned_ids(evidence) == ["ATL-112"]  # THR-201 is not a registered source prefix
    _, cites, warns = v.verify_citations("See [THR-201].", [], known_ids=v.load_known_ids(), ev_ids=set())
    assert cites == [] and "unknown_ticket_id:THR-201" in warns
