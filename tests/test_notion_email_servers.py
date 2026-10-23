"""Notion and email server tests (D3, D4). No AI; the real subprocess round trip is at the end."""
import asyncio
import json
from pathlib import Path

import pytest

from app.mcp_servers import email_server as em
from app.mcp_servers import notion_server as nt

FIX = Path(__file__).parent / "fixtures"
SYN = Path(__file__).parents[1] / "data" / "synthetic"


@pytest.fixture(autouse=True)
def stub_data(monkeypatch):
    monkeypatch.setenv("NOTION_DATA_PATH", str(SYN / "notion_pages_stub.json"))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(SYN / "email_messages_stub.json"))


# ---------------------------------------------------------------- notion
def test_search_pages_returns_best_matching_section():
    out = nt.search_pages("refund webhooks")
    assert out["count"] >= 1
    row = out["results"][0]
    assert {"id", "title", "page_type", "status", "last_edited", "section"} <= set(row)
    assert set(row["section"]) == {"heading", "text"} and "refund" in row["section"]["text"].lower()
    assert len(row["section"]["text"]) <= nt.SECTION_CHARS + 1


def test_search_pages_all_tokens_must_match_and_star_matches_all():
    assert nt.search_pages("continuous integration")["count"] == 1
    assert nt.search_pages("continuous refund")["count"] == 0  # every token must match
    assert nt.search_pages("*")["count"] == 3
    assert nt.search_pages("zzzznothing")["count"] == 0


def test_search_pages_newest_first_and_capped(tmp_path, monkeypatch):
    many = [{"id": f"NTN-{i:03d}", "title": "t", "last_edited": f"2026-01-{i % 28 + 1:02d}",
             "sections": [{"heading": "h", "text": "word"}]} for i in range(25)]
    p = tmp_path / "p.json"; p.write_text(json.dumps(many))
    monkeypatch.setenv("NOTION_DATA_PATH", str(p))
    out = nt.search_pages("word")
    assert out["count"] == 25 and len(out["results"]) == nt.MAX_RESULTS and out["truncated"] is True


def test_get_page_full_with_status_and_date():
    page = nt.get_page("ntn-104")
    assert page["id"] == "NTN-104" and page["last_edited"] == "2026-06-18" and page["status"] == "Approved"
    assert len(page["sections"]) == 4 and "version" not in page  # the data has no version field
    assert nt.get_page("105")["id"] == "NTN-105"


def test_get_page_passes_a_version_through_when_a_record_has_one(tmp_path, monkeypatch):
    p = tmp_path / "p.json"; p.write_text(json.dumps([{"id": "NTN-001", "title": "t", "version": 7, "sections": []}]))
    monkeypatch.setenv("NOTION_DATA_PATH", str(p))
    assert nt.get_page("NTN-001")["version"] == 7


def test_get_page_size_capped(tmp_path, monkeypatch):
    big = [{"id": "NTN-001", "title": "t", "last_edited": "2026-01-01", "sections": [{"heading": "a", "text": "x" * 5000}, {"heading": "b", "text": "y" * 5000}]}]
    p = tmp_path / "p.json"; p.write_text(json.dumps(big))
    monkeypatch.setenv("NOTION_DATA_PATH", str(p))
    page = nt.get_page("NTN-001")
    assert page["truncated"] is True
    assert sum(len(s["text"]) for s in page["sections"]) <= nt.MAX_PAGE_CHARS + 4


@pytest.mark.parametrize("fn,arg", [(nt.search_pages, ""), (nt.search_pages, None), (nt.get_page, " "), (nt.get_page, None)])
def test_notion_bad_input_is_an_error_object(fn, arg):
    assert fn(arg)["error"] == "invalid_input"


def test_notion_not_found_and_missing_data(monkeypatch):
    assert nt.get_page("NTN-999") == {"error": "not_found", "id": "NTN-999"}
    monkeypatch.setenv("NOTION_DATA_PATH", "/nonexistent/x.json")
    assert nt.search_pages("a")["error"] == "data_unavailable"
    assert nt.get_page("NTN-104")["error"] == "data_unavailable"


def test_notion_corrupt_data_is_an_error_object(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"; p.write_text("{not json")
    monkeypatch.setenv("NOTION_DATA_PATH", str(p))
    assert nt.search_pages("a")["error"] == "data_unavailable"


# ----------------------------------------------------------------- email
def test_search_emails_snippet_is_the_first_160_chars_of_the_body_only():
    out = em.search_emails("*")
    assert out["count"] == 3
    full = {m["id"]: m["body"] for m in json.loads((SYN / "email_messages_stub.json").read_text())}
    for r in out["results"]:
        assert r["snippet"] == full[r["id"]][:160] and "body" not in r
    # a hit deep in the body still returns the START of the body, never a window around the match
    hit = em.search_emails("liability")["results"][0]
    assert hit["snippet"] == full[hit["id"]][:160]
    dates = [r["date"] for r in out["results"]]
    assert dates == sorted(dates, reverse=True)


def test_get_email_and_numeric_lookup():
    m = em.get_email("EML-111")
    assert m["thread_id"] == "THR-202" and m["in_reply_to"] == "EML-110" and "Sandbox" in m["body"]
    assert em.get_email("144")["id"] == "EML-144"
    assert em.get_email("EML-404") == {"error": "not_found", "id": "EML-404"}


def test_get_thread_accepts_a_thread_id_or_an_email_id():
    t = em.get_thread("THR-202")
    assert t["thread_id"] == "THR-202" and t["count"] == 1 and t["messages"][0]["id"] == "EML-111"
    assert em.get_thread("EML-111")["thread_id"] == "THR-202"
    assert em.get_thread("THR-999")["error"] == "not_found"


def test_get_thread_order_does_not_depend_on_file_order(tmp_path, monkeypatch):
    rows = [{"id": "EML-002", "thread_id": "t", "date": "2026-02-02T09:00:00", "body": "second"},
            {"id": "EML-001", "thread_id": "t", "date": "2026-02-02T08:00:00", "body": "first"}]
    p = tmp_path / "e.json"; p.write_text(json.dumps(rows))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(p))
    assert [m["id"] for m in em.get_thread("t")["messages"]] == ["EML-001", "EML-002"]


def test_email_size_caps(tmp_path, monkeypatch):
    rows = [{"id": f"EML-{i:03d}", "thread_id": "t", "date": f"2026-01-0{i}", "body": "z" * 3500} for i in range(1, 6)]
    rows[0]["body"] = "z" * 9000
    p = tmp_path / "e.json"; p.write_text(json.dumps(rows))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(p))
    assert em.get_email("EML-001")["truncated"] is True
    t = em.get_thread("t")
    assert t["truncated"] is True and sum(len(m["body"]) for m in t["messages"]) <= em.MAX_THREAD_CHARS + 8


@pytest.mark.parametrize("fn,arg", [(em.search_emails, ""), (em.get_email, None), (em.get_thread, "  ")])
def test_email_bad_input_is_an_error_object(fn, arg):
    assert fn(arg)["error"] == "invalid_input"


def test_email_missing_data(monkeypatch):
    monkeypatch.setenv("EMAIL_DATA_PATH", "/nonexistent/x.json")
    assert em.search_emails("a")["error"] == "data_unavailable"
    assert em.get_thread("t")["error"] == "data_unavailable"


def test_servers_are_read_only_and_never_print():
    for mod in (nt, em):
        src = Path(mod.__file__).read_text()
        assert "print(" not in src and "open(" not in src.replace("with open(path", "")  # no writes, no stdout
        assert "'w'" not in src and '"w"' not in src


# ---------------------------------------------------------- real subprocesses
def test_three_servers_through_the_router_with_real_subprocesses():
    from app import sources
    from app.nodes.mcp_client import McpRouter

    async def go():
        srcs = [sources.JIRA, sources.NOTION, sources.EMAIL]
        env = {"jira": {"JIRA_DATA_PATH": str(FIX / "jira_stub.json")},
               "notion": {"NOTION_DATA_PATH": str(SYN / "notion_pages_stub.json")},
               "email": {"EMAIL_DATA_PATH": str(SYN / "email_messages_stub.json")}}
        async with McpRouter(srcs, env=env, startup_timeout=20) as r:
            assert r.unavailable == {}
            assert {t["name"] for t in r.tools} == {n for s in srcs for n in s.tool_names}
            page = await r.call("get_page", {"page_id": "NTN-104"})
            mail = await r.call("get_thread", {"thread_id": "THR-202"})
            jira = await r.call("get_ticket", {"ticket_id": "ATL-112"})
            assert page.ok and page.data["id"] == "NTN-104"
            assert mail.ok and mail.data["count"] == 1
            assert jira.ok and jira.data["id"] == "ATL-112"

    asyncio.run(go())
