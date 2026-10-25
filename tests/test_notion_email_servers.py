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


def test_search_pages_ranks_by_how_many_words_match_and_star_matches_all():
    out = nt.search_pages("continuous integration")  # one word per page: neither page is excluded any more
    ids = [r["id"] for r in out["results"]]
    assert {"NTN-112", "NTN-104"} <= set(ids) and ids[0] == "NTN-104"  # a title hit outranks a body hit
    assert nt.search_pages("zzzznothing")["count"] == 0
    assert nt.search_pages("*")["count"] == 3


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
    assert t["thread_id"] == "THR-202" and t["count"] == 1 and t["emails"][0]["id"] == "EML-111"
    assert t["emails"][0]["thread_id"] == "THR-202"  # contract: full messages, thread_id included
    assert em.get_thread("EML-111")["thread_id"] == "THR-202"
    assert em.get_thread("THR-999")["error"] == "not_found"


def test_get_thread_order_does_not_depend_on_file_order(tmp_path, monkeypatch):
    rows = [{"id": "EML-002", "thread_id": "t", "date": "2026-02-02T09:00:00", "body": "second"},
            {"id": "EML-001", "thread_id": "t", "date": "2026-02-02T08:00:00", "body": "first"}]
    p = tmp_path / "e.json"; p.write_text(json.dumps(rows))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(p))
    assert [m["id"] for m in em.get_thread("t")["emails"]] == ["EML-001", "EML-002"]


def test_email_size_caps(tmp_path, monkeypatch):
    rows = [{"id": f"EML-{i:03d}", "thread_id": "t", "date": f"2026-01-0{i}", "body": "z" * 3500} for i in range(1, 6)]
    rows[0]["body"] = "z" * 9000
    p = tmp_path / "e.json"; p.write_text(json.dumps(rows))
    monkeypatch.setenv("EMAIL_DATA_PATH", str(p))
    assert em.get_email("EML-001")["truncated"] is True
    t = em.get_thread("t")
    assert t["truncated"] is True and sum(len(m["body"]) for m in t["emails"]) <= em.MAX_THREAD_CHARS + 8


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


# ------------------------------------------------------------- ranked search (D, after the M2 retrieval check)
def _write(tmp_path, monkeypatch, var, rows):
    p = tmp_path / "d.json"
    p.write_text(json.dumps(rows))
    monkeypatch.setenv(var, str(p))


def test_a_question_shaped_query_is_not_empty_any_more(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T-1", "subject": "Sandbox provisioning timeline", "from": "a", "date": "2026-01-01", "body": "Twenty days."},
        {"id": "EML-002", "thread_id": "T-2", "subject": "Lunch", "from": "b", "date": "2026-02-01", "body": "Pizza on Friday."}])
    out = em.search_emails("how long does the vendor say sandbox provisioning takes once signed")
    assert out["results"][0]["id"] == "EML-001"   # the old all-words-must-match rule returned nothing here


def test_more_matching_words_and_subject_hits_rank_first(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T", "subject": "Misc", "from": "a", "date": "2026-03-01", "body": "budget review notes"},
        {"id": "EML-002", "thread_id": "T", "subject": "Budget review", "from": "a", "date": "2026-01-01", "body": "x"},
        {"id": "EML-003", "thread_id": "T", "subject": "Misc", "from": "a", "date": "2026-02-01", "body": "budget"}])
    ids = [r["id"] for r in em.search_emails("budget review")["results"]]
    assert ids == ["EML-002", "EML-001", "EML-003"]   # subject hits triple; then two words beat one; newest not first


def test_equal_scores_fall_back_to_newest_first(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": f"EML-00{i}", "thread_id": "T", "subject": "s", "from": "a", "date": f"2026-0{i}-01", "body": "same word"} for i in (1, 2, 3)])
    assert [r["id"] for r in em.search_emails("word")["results"]] == ["EML-003", "EML-002", "EML-001"]


def test_word_forms_match_by_stem(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T", "subject": "Escalation: vendor timeline", "from": "a", "date": "2026-01-01", "body": "policies delayed"}])
    for kw in ("escalated", "escalate", "delay", "delays", "policy", "vendors"):
        assert em.search_emails(kw)["count"] == 1, kw
    assert em.search_emails("budget")["count"] == 0
    assert em.search_emails("cat")["count"] == 0     # short words must match exactly (no prefix guessing)


def test_ids_and_dates_stay_exact(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T", "subject": "s", "from": "a", "date": "2026-01-01", "body": "see ATL-112 on 2026-06-22"}])
    assert em.search_emails("ATL-112")["count"] == 1 and em.search_emails("2026-06-22")["count"] == 1
    assert em.search_emails("ATL-11")["count"] == 0 and em.search_emails("2026-06-2")["count"] == 0


def test_the_snippet_is_still_only_the_start_of_the_body_after_ranking(tmp_path, monkeypatch):
    body = "a" * 200 + " hidden payload"
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T", "subject": "s", "from": "a", "date": "2026-01-01", "body": body}])
    r = em.search_emails("payload")["results"][0]
    assert r["snippet"] == body[:160] and "payload" not in r["snippet"]


def test_pages_rank_title_hits_first_and_return_the_best_section(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "NOTION_DATA_PATH", [
        {"id": "NTN-001", "title": "Misc", "last_edited": "2026-03-01", "sections": [{"heading": "h", "text": "launch scope decision"}]},
        {"id": "NTN-002", "title": "Launch scope", "last_edited": "2026-01-01", "sections": [
            {"heading": "Intro", "text": "nothing"}, {"heading": "Decision", "text": "launch scope was cut"}]}])
    out = nt.search_pages("why was the launch scope cut")
    assert [r["id"] for r in out["results"]] == ["NTN-002", "NTN-001"]
    assert out["results"][0]["section"]["heading"] == "Decision"


def test_star_and_filler_only_queries_list_newest_first(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "EMAIL_DATA_PATH", [
        {"id": "EML-001", "thread_id": "T", "subject": "s", "from": "a", "date": "2026-01-01", "body": "x"},
        {"id": "EML-002", "thread_id": "T", "subject": "s", "from": "a", "date": "2026-02-01", "body": "y"}])
    for kw in ("*", "the of"):
        assert [r["id"] for r in em.search_emails(kw)["results"]] == ["EML-002", "EML-001"]


def test_question_shaped_queries_reach_the_documents_the_tuning_key_expects():
    """Retrieval floor on A's real data and the TUNING key only: with the question's content words as the query,
    each expected Notion/email record must rank in the top 10. (Measured: 16 of 17; the old rule gave 0 of 17.)"""
    key_path, em_path, nt_path = (Path(__file__).parents[1] / "evals" / "phase2_key.json",
                                  SYN / "email_messages.json", SYN / "notion_pages.json")
    if not (key_path.exists() and em_path.exists() and nt_path.exists()):
        pytest.skip("tuning key or A's full data is not present")
    from app.mcp_servers import _common as c
    import os
    os.environ["EMAIL_DATA_PATH"], os.environ["NOTION_DATA_PATH"] = str(em_path), str(nt_path)
    total = reached = 0
    for q in json.loads(key_path.read_text(encoding="utf-8")):
        query = " ".join(c.tokens(q["question"]))
        for cid in q.get("expected_citations", []):
            if cid.startswith(("EML", "NTN")):
                fn = em.search_emails if cid.startswith("EML") else nt.search_pages
                total += 1
                reached += cid in [r["id"] for r in fn(query)["results"]]
    assert total >= 15 and reached >= total - 2, (reached, total)
