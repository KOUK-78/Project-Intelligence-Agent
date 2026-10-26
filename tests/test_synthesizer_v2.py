"""B5/B7: synthesizer v2. Grouping, any-source citations, conflicts, and honesty about gaps."""
import asyncio

from app import config
from app.nodes.llm import LLMReply
from app.nodes.synthesizer import (SYNTH_V2_SYSTEM, ensure_gap_notes, format_grouped_evidence, gap_answer, gaps_of,
                                   make_synthesizer_v2_node)
from tests.fakes import FakeLLM, synth_reply

PLAN = [
    {"id": "sq1", "question": "what does the tracker say", "source": "jira", "reason": ""},
    {"id": "sq2", "question": "what does the wiki say", "source": "notion", "reason": ""},
    {"id": "sq3", "question": "what does the mailbox say", "source": "email", "reason": ""},
]


def item(sq, source, rid, tool="search_tickets", ok=True, data=None):
    return {"tool": tool, "args": {"keyword": "k"}, "ok": ok, "data": data if data is not None else {"id": rid},
            "error": None if ok else "boom", "sq": sq, "source": source}


def res(sq, status, unavailable=(), reason="model", question=None):
    q = question or next(p["question"] for p in PLAN if p["id"] == sq)
    return {"id": sq, "question": q, "source": next(p["source"] for p in PLAN if p["id"] == sq), "status": status,
            "reason": reason, "steps": 1, "attempts": 1, "rewritten": None, "capped": False, "unavailable": list(unavailable)}


EVIDENCE = [item("sq1", "jira", "ATL-001"), item("sq2", "notion", "NTN-001", tool="get_page"),
            item("sq3", "email", "EML-001", tool="get_email")]
ALL_OK = [res("sq1", "resolved"), res("sq2", "resolved"), res("sq3", "resolved")]


def synth(llm, results=ALL_OK, evidence=EVIDENCE, plan=PLAN, known=None, question="Q?"):
    node = make_synthesizer_v2_node(llm, known)
    return asyncio.run(node({"question": question, "plan": plan, "sq_results": results, "evidence": evidence}))


# ---------------------------------------------------------------- the prompt

def test_prompt_is_source_neutral_and_carries_the_rules():
    for needle in ("in brackets", "exact ID printed in the evidence", "BOTH values", "NOT ENOUGH EVIDENCE", "SOURCE UNAVAILABLE"):
        assert needle in SYNTH_V2_SYSTEM, needle
    assert "JSON" in SYNTH_V2_SYSTEM and config.TODAY in SYNTH_V2_SYSTEM
    for placeholder in ("ATL-000", "NTN-000", "EML-000"):  # a model copies example IDs into its answer
        assert placeholder not in SYNTH_V2_SYSTEM


def test_evidence_is_grouped_by_sub_question_with_source_and_question():
    text = format_grouped_evidence(PLAN, ALL_OK, EVIDENCE)
    heads = [line for line in text.splitlines() if line.startswith("Sub-question")]
    assert heads == ["Sub-question sq1 (source: jira): what does the tracker say",
                     "Sub-question sq2 (source: notion): what does the wiki say",
                     "Sub-question sq3 (source: email): what does the mailbox say"]
    assert text.index("ATL-001") < text.index("sq2") < text.index("NTN-001") < text.index("sq3") < text.index("EML-001")


def test_an_empty_or_unavailable_group_says_so_in_capitals():
    results = [res("sq1", "resolved"), res("sq2", "unresolved", reason="model"), res("sq3", "unavailable", ["email"])]
    text = format_grouped_evidence(PLAN, results, [item("sq1", "jira", "ATL-001")])
    sq2 = text.split("Sub-question sq2")[1].split("Sub-question sq3")[0]
    assert "NOT ENOUGH EVIDENCE" in sq2 and "NTN" not in sq2
    assert "SOURCE UNAVAILABLE: email" in text.split("Sub-question sq3")[1]


def test_an_unresolved_group_that_has_records_shows_them_and_the_warning():
    text = format_grouped_evidence(PLAN[:1], [res("sq1", "unresolved")], [item("sq1", "jira", "ATL-001")])
    assert "ATL-001" in text and "NOT ENOUGH EVIDENCE" in text


def test_a_resolved_group_with_a_source_down_still_carries_the_notice():
    text = format_grouped_evidence(PLAN[:1], [res("sq1", "resolved", ["notion"])], [item("sq1", "jira", "ATL-001")])
    assert "ATL-001" in text and "SOURCE UNAVAILABLE: notion" in text


def test_each_group_gets_a_share_of_the_evidence_budget(monkeypatch):
    monkeypatch.setattr(config, "EVIDENCE_TOTAL_CHARS_PLANNED", 600)
    big = {"id": "ATL-001", "blob": "x" * 5000}
    ev = [item(p["id"], p["source"], "ATL-001", data=big) for p in PLAN for _ in range(3)]
    text = format_grouped_evidence(PLAN, ALL_OK, ev)
    assert "omitted for length" in text and len(text) < 3000
    assert all(f"Sub-question {p['id']}" in text for p in PLAN)  # no group is crowded out


# ---------------------------------------------------------------- answers and citations

def test_cites_ids_from_every_source_and_the_verifier_keeps_them():
    llm = FakeLLM(synthesizer=[synth_reply("Tracker [ATL-001], wiki [NTN-001], mail [EML-001].",
                                           ["ATL-001", "NTN-001", "EML-001"])])
    out = synth(llm)
    assert out["status"] == "ok" and out["citations"] == ["ATL-001", "NTN-001", "EML-001"]
    assert "unverified" not in out["answer"]


def test_a_conflict_across_sources_keeps_both_values_and_both_citations():
    llm = FakeLLM(synthesizer=[synth_reply("The ticket says 12 June [ATL-001] but the wiki says 19 June [NTN-001].",
                                           ["ATL-001", "NTN-001"])])
    out = synth(llm)
    assert out["citations"] == ["ATL-001", "NTN-001"] and "12 June" in out["answer"] and "19 June" in out["answer"]
    user = llm.calls["synthesizer"][0][1]["content"]
    assert "ATL-001" in user and "NTN-001" in user  # both sides were in front of the model


def test_an_id_that_is_not_in_the_evidence_is_flagged_not_cited():
    llm = FakeLLM(synthesizer=[synth_reply("Also [NTN-999] and [ATL-001].", ["NTN-999", "ATL-001"])])
    out = synth(llm, known={"ATL-001", "NTN-001", "EML-001", "NTN-999"})
    assert out["citations"] == ["ATL-001"] and "[unverified: NTN-999]" in out["answer"]
    assert "not_in_evidence:NTN-999" in out["warnings"]


def test_bare_ids_are_bracketed_as_in_phase1():
    out = synth(FakeLLM(synthesizer=[synth_reply("See NTN-001.", ["NTN-001"])]))
    assert out["answer"] == "See [NTN-001]." and out["citations"] == ["NTN-001"]


# ---------------------------------------------------------------- honesty about gaps

def test_nothing_found_anywhere_is_a_deterministic_answer_with_no_model_call():
    llm = FakeLLM(synthesizer=[synth_reply("never used", [])])
    results = [res("sq1", "unresolved", reason="no_records"), res("sq2", "unavailable", ["notion"]),
               res("sq3", "unavailable", ["email"])]
    out = synth(llm, results=results, evidence=[])
    assert llm.count("synthesizer") == 0 and out["citations"] == [] and out["status"] == "ok"
    assert out["answer"] == "There is not enough evidence to answer that. Source unavailable: notion, email."


def test_records_without_ids_do_not_count_as_evidence():
    llm = FakeLLM(synthesizer=[synth_reply("x", [])])
    out = synth(llm, results=[res("sq1", "unresolved")], evidence=[item("sq1", "jira", "", data={"count": 0})])
    assert llm.count("synthesizer") == 0 and out["answer"] == gap_answer([])


def test_missing_notes_are_added_when_the_model_skipped_them():
    llm = FakeLLM(synthesizer=[synth_reply("Tracker says so [ATL-001].", ["ATL-001"])])
    results = [res("sq1", "resolved"), res("sq2", "unresolved", question="what does the wiki say"),
               res("sq3", "unavailable", ["email"])]
    out = synth(llm, results=results, evidence=[item("sq1", "jira", "ATL-001")])
    assert out["answer"] == ("Tracker says so [ATL-001]. Source unavailable: email. "
                             "Not enough evidence for: what does the wiki say.")
    assert out["citations"] == ["ATL-001"]


def test_notes_the_model_already_wrote_are_not_duplicated():
    said = "Tracker [ATL-001]. Not enough evidence about the wiki. Source unavailable: email."
    llm = FakeLLM(synthesizer=[synth_reply(said, ["ATL-001"])])
    results = [res("sq1", "resolved"), res("sq2", "unresolved"), res("sq3", "unavailable", ["email"])]
    assert synth(llm, results=results, evidence=[item("sq1", "jira", "ATL-001")])["answer"] == said


def test_ensure_gap_notes_is_case_insensitive_and_a_no_op_without_gaps():
    assert ensure_gap_notes("a", [], []) == "a"
    assert ensure_gap_notes("source UNAVAILABLE: x", ["x"], []) == "source UNAVAILABLE: x"


def test_gaps_of_collects_unique_sources_unresolved_questions_and_budget():
    results = [res("sq1", "unavailable", ["email"]), res("sq2", "unresolved", reason="budget_exhausted"),
               res("sq3", "resolved", ["email", "notion"])]
    assert gaps_of(results) == (["email", "notion"], ["what does the wiki say"], True)
    assert gaps_of([res("sq1", "resolved")]) == ([], [], False)


def test_budget_exhaustion_is_reported_as_loop_cap_but_still_answers():
    llm = FakeLLM(synthesizer=[synth_reply("Tracker [ATL-001].", ["ATL-001"])])
    results = [res("sq1", "resolved"), res("sq2", "unresolved", reason="budget_exhausted")]
    out = synth(llm, results=results, evidence=[item("sq1", "jira", "ATL-001")], plan=PLAN[:2])
    assert out["status"] == "loop_cap" and "Not enough evidence for" in out["answer"]
    assert synth(FakeLLM(synthesizer=[synth_reply("x", [])]), results=[res("sq1", "unresolved", reason="budget_exhausted")],
                 evidence=[], plan=PLAN[:1])["status"] == "loop_cap"


# ---------------------------------------------------------------- failure paths (same as Phase 1)

def test_invalid_json_is_retried_once_then_invalid_output():
    llm = FakeLLM(synthesizer=[LLMReply(content="no"), LLMReply(content="still no")])
    out = synth(llm)
    assert llm.count("synthesizer") == 2 and out["status"] == "invalid_output" and out["citations"] == []
    assert len(out["errors"]) == 2


def test_an_unreachable_model_is_status_error():
    class Boom:
        async def chat(self, *a, **k):
            raise ConnectionError("down")

    out = synth(Boom())
    assert out["status"] == "error" and out["errors"][0].startswith("llm_unavailable")


# ---------------------------------------------------------------- sources whose data file could not be read

def test_ids_of_an_unreadable_source_are_checked_against_the_evidence_only():
    answer = "Tracker [ATL-001], wiki [NTN-001]."
    reply = synth_reply(answer, ["ATL-001", "NTN-001"])
    known = {"ATL-001"}  # the Notion data could not be read, so its IDs are not in the known set
    strict = asyncio.run(make_synthesizer_v2_node(FakeLLM(synthesizer=[reply]), known)(
        {"question": "Q?", "plan": PLAN, "sq_results": ALL_OK, "evidence": EVIDENCE}))
    assert strict["citations"] == ["ATL-001"] and "[unverified: NTN-001]" in strict["answer"]
    lenient = asyncio.run(make_synthesizer_v2_node(FakeLLM(synthesizer=[reply]), known, {"NTN"})(
        {"question": "Q?", "plan": PLAN, "sq_results": ALL_OK, "evidence": EVIDENCE}))
    assert lenient["citations"] == ["ATL-001", "NTN-001"] and "unverified" not in lenient["answer"]


def test_an_unchecked_prefix_does_not_excuse_an_id_that_is_not_in_the_evidence():
    llm = FakeLLM(synthesizer=[synth_reply("Tracker [ATL-001] and [NTN-999].", ["ATL-001", "NTN-999"])])
    out = asyncio.run(make_synthesizer_v2_node(llm, {"ATL-001"}, {"NTN"})(
        {"question": "Q?", "plan": PLAN, "sq_results": ALL_OK, "evidence": EVIDENCE}))
    assert out["citations"] == ["ATL-001"] and "[unverified: NTN-999]" in out["answer"]
    assert "not_in_evidence:NTN-999" in out["warnings"]


def test_records_of_an_unresolved_sub_question_are_marked_as_possibly_unrelated():
    """A keyword search that found something unrelated must not be reported as the answer (planner-on regression)."""
    from app.nodes.synthesizer import SYNTH_V2_SYSTEM, format_grouped_evidence
    plan = [{"id": "sq1", "question": "Q?", "source": "jira", "reason": "r"}]
    results = [{"id": "sq1", "status": "unresolved", "reason": "insufficient", "unavailable": []}]
    from app.nodes.state import tag_evidence
    evidence = tag_evidence([{"tool": "search_tickets", "args": {}, "ok": True,
                              "data": {"results": [{"id": "ATL-001"}]}, "error": None}], "sq1", "jira")
    text = format_grouped_evidence(plan, results, evidence)
    assert "NOT ENOUGH EVIDENCE" in text and "unrelated" in text and "ATL-001" in text
    assert "do not cite them" in SYNTH_V2_SYSTEM


def test_a_short_group_donates_its_unused_share_to_a_long_one():
    from app.nodes.synthesizer import group_shares
    assert group_shares([100, 5000, 5000], 3000) == [100, 1450, 1450]
    assert group_shares([5000, 5000], 3000) == [1500, 1500]
    assert group_shares([10, 20], 3000) == [10, 20]  # nobody gets more than it needs
    assert sum(group_shares([900, 4000, 4000, 50], 6000)) <= 6000


def _resolved_state(status):
    from app.nodes.state import tag_evidence
    plan = [{"id": "sq1", "question": "Q?", "source": "email", "reason": "r"}]
    results = [{"id": "sq1", "status": status, "reason": "model", "unavailable": [], "steps": 1, "attempts": 1,
                "rewritten": None, "capped": False, "question": "Q?"}]
    ev = tag_evidence([{"tool": "get_thread", "args": {}, "ok": True,
                        "data": {"emails": [{"id": "EML-001"}]}, "error": None}], "sq1", "email")
    return {"question": "Q?", "plan": plan, "sq_results": results, "evidence": ev}


def test_an_answer_that_cites_nothing_is_asked_once_for_its_ids_when_a_sub_question_resolved():
    llm = FakeLLM(synthesizer=[synth_reply("Owen escalated it.", []), synth_reply("Owen escalated it [EML-001].", ["EML-001"])])
    out = asyncio.run(make_synthesizer_v2_node(llm, {"EML-001"})(_resolved_state("resolved")))
    assert llm.count("synthesizer") == 2 and out["citations"] == ["EML-001"]


def test_no_citation_nudge_when_nothing_resolved_or_the_retry_still_cites_nothing():
    llm = FakeLLM(synthesizer=[synth_reply("Not found.", [])])
    out = asyncio.run(make_synthesizer_v2_node(llm, {"EML-001"})(_resolved_state("unresolved")))
    assert llm.count("synthesizer") == 1 and out["citations"] == []
    llm = FakeLLM(synthesizer=[synth_reply("Owen escalated it.", [])])
    out = asyncio.run(make_synthesizer_v2_node(llm, {"EML-001"})(_resolved_state("resolved")))
    assert llm.count("synthesizer") == 2 and out["answer"].startswith("Owen escalated it.") and out["citations"] == []


def test_full_records_come_before_search_listings_and_threads_are_plain_text():
    from app.nodes.synthesizer import format_group_evidence
    listing = {"tool": "search_emails", "args": {}, "ok": True, "error": None,
               "data": {"results": [{"id": "EML-001", "snippet": "x" * 1500}]}}
    thread = {"tool": "get_thread", "args": {"thread_id": "THR-1"}, "ok": True, "error": None,
              "data": {"thread_id": "THR-1", "count": 1,
                       "emails": [{"id": "EML-002", "date": "d", "from": "a", "subject": "s", "body": "the fee applies"}]}}
    text = format_group_evidence([listing, thread], 400)
    assert "get_thread" in text and "omitted for length" in text  # the long listing is what gets cut, not the thread
    assert "[EML-002]" in text and "the fee applies" in text and '"emails"' not in text
