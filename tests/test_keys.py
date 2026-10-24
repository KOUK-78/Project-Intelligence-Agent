"""A4 / C6: every answer file is valid against the data, and the reserved fixtures stay out of all of them.

Covers the Phase 1 keys and, when A has written them, the Phase 2 keys (evals/phase2_key.json and
evals/phase2_heldout_key.json). Item IDs are checked against the data of the source their prefix names.
Reads the held-out keys only to check FORMAT, IDs and reserved text. Nothing here tunes anything against them,
and no assertion message quotes held-out wording.
"""
import json
import re
from pathlib import Path

import pytest

from app import config
from evals import run_evals as ev
from tests import corpus

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synthetic" / "jira_tickets.json"
PHASE1_KEYS = {"answer_key": ROOT / "evals" / "answer_key.json",
               "heldout_key": ROOT / "evals" / "heldout_key.json"}
PHASE2_KEYS = {"phase2_key": ROOT / "evals" / "phase2_key.json",
               "phase2_heldout_key": ROOT / "evals" / "phase2_heldout_key.json"}
# Phase 2 keys are A's deliverable; until a file exists its tests are simply absent, not failing.
KEY_PATHS = {**PHASE1_KEYS, **{n: p for n, p in PHASE2_KEYS.items() if p.exists()}}
HELDOUT = {"heldout_key", "phase2_heldout_key"}

# Phase 4 (injection) and Phase 5 (PII) fixtures: never used by any question, in any source.
# data/reserved_fixtures.json (A) adds the Notion and email ones; the two Jira fixtures are always reserved.
RESERVED = corpus.reserved()["ids"]
# Anything that identifies those tickets, so a rewording can't smuggle them in.
RESERVED_TEXT = sorted(corpus.reserved()["phrases"])
ID_RE = re.compile(r"^(ATL|NTN|EML)-\d{3}$")


def tickets():
    return json.loads(DATA.read_text(encoding="utf-8"))


def ids():
    """Every real item ID, across all sources whose data exists."""
    return {t["id"] for t in tickets()} | corpus.facts()["ids"]


def shown(name, q, field):
    """Where a problem is, without quoting held-out wording."""
    return f"{name} {q['id']}.{field}"


def load(name):
    return json.loads(KEY_PATHS[name].read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(KEY_PATHS))
def key_name(request):
    return request.param


def test_key_loads_with_the_eval_runners_own_validator(key_name):
    assert ev.load_key(KEY_PATHS[key_name])


def test_every_expected_and_forbidden_id_exists_in_the_data(key_name):
    valid = ids()
    for q in load(key_name):
        for field in ("expected_citations", "forbidden_citations"):
            for cid in q.get(field, []):
                assert ID_RE.match(cid), f"{key_name} {q['id']}.{field}: malformed ID {cid!r}"
                assert cid in valid, f"{key_name} {q['id']}.{field}: {cid} is not an item in the data"


def test_expected_and_forbidden_never_overlap_and_have_no_duplicates(key_name):
    for q in load(key_name):
        exp, forb = q["expected_citations"], q.get("forbidden_citations", [])
        assert len(set(exp)) == len(exp) and len(set(forb)) == len(forb), q["id"]
        assert not set(exp) & set(forb), f"{key_name} {q['id']}: {sorted(set(exp) & set(forb))} both expected and forbidden"


def test_reserved_fixtures_are_not_in_any_key(key_name):
    raw = KEY_PATHS[key_name].read_text(encoding="utf-8")
    for q in load(key_name):
        used = (set(q["expected_citations"]) | set(q.get("forbidden_citations", []))) & RESERVED
        assert not used, f"{key_name} {q['id']} uses reserved {sorted(used)}"
    for rid in RESERVED:
        assert rid not in raw, f"{rid} appears in the text of {KEY_PATHS[key_name].name}"
    low = raw.lower()
    for phrase in RESERVED_TEXT:
        assert phrase not in low, f"a reserved-fixture phrase appears in {KEY_PATHS[key_name].name}"  # phrase withheld


def test_key_sizes():
    assert 8 <= len(load("answer_key")) <= 10
    assert 8 <= len(load("heldout_key")) <= 10
    for name in PHASE2_KEYS:
        if name in KEY_PATHS:
            assert 5 <= len(load(name)) <= 20, f"{name}: {len(load(name))} questions"


def test_heldout_is_disjoint_from_the_tuning_key():
    tune, held = load("answer_key"), load("heldout_key")
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    assert not {q["id"] for q in tune} & {q["id"] for q in held}
    assert not {norm(q["question"]) for q in tune} & {norm(q["question"]) for q in held}
    assert all(q["id"].startswith("H") for q in held) and all(q["id"].startswith("Q") for q in tune)


@pytest.mark.skipif(not {"phase2_key", "phase2_heldout_key"} <= set(KEY_PATHS), reason="Phase 2 keys not written yet")
def test_phase2_heldout_is_disjoint_from_every_tuning_key():
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    held = load("phase2_heldout_key")
    tuning = load("answer_key") + load("phase2_key") + load("heldout_key")
    assert not {q["id"] for q in held} & {q["id"] for q in tuning}, "question ids overlap"
    assert not {norm(q["question"]) for q in held} & {norm(q["question"]) for q in tuning}, "a question is repeated"
    assert len({q["id"] for q in held}) == len(held)


def test_heldout_covers_the_due_today_boundary():
    """ATL-116 is due exactly on the frozen TODAY and is NOT overdue; the held-out set must test that."""
    assert any("ATL-116" in q["expected_citations"] for q in load("heldout_key"))


def test_forbidden_ids_name_a_real_wrong_answer_not_a_random_ticket():
    """Every forbidden ID must be a ticket that exists AND is not what the question's expected answer cites."""
    valid = ids()
    for name in KEY_PATHS:
        for q in load(name):
            for fid in q.get("forbidden_citations", []):
                assert fid in valid and fid not in q["expected_citations"], (name, q["id"], fid)


# ---------------------------------------------------------------- Phase 2 optional fields (CONTRACTS.md section 5)
def phase2_names():
    return [n for n in PHASE2_KEYS if n in KEY_PATHS]


@pytest.fixture(params=phase2_names() or [pytest.param("none", marks=pytest.mark.skip(reason="Phase 2 keys not written yet"))])
def p2_key(request):
    return request.param


def test_sources_required_are_real_sources_covered_by_expected_citations(p2_key):
    for q in load(p2_key):
        prefixes = {ev.source_of(c) for c in q["expected_citations"]}
        for source in q.get("sources_required", []):
            assert source in config.SOURCES, f"{shown(p2_key, q, 'sources_required')}: unknown source {source!r}"
            assert source in prefixes, f"{shown(p2_key, q, 'sources_required')}: no expected citation from {source}"


def test_must_mention_strings_are_checkable_against_the_expected_answer(p2_key):
    for q in load(p2_key):
        answer = ev.normalize_text(q["expected_answer"])
        for text in q.get("must_mention", []):
            assert isinstance(text, str) and text.strip(), shown(p2_key, q, "must_mention")
            assert ev.normalize_text(text) in answer, \
                f"{shown(p2_key, q, 'must_mention')}: a required string is not in expected_answer, so a correct answer could fail"
            assert len(ev.normalize_text(text)) >= 3, f"{shown(p2_key, q, 'must_mention')}: too short to be a meaningful check"


def test_phase2_questions_ask_for_more_than_one_source_somewhere(p2_key):
    multi = [q for q in load(p2_key) if len(q.get("sources_required", [])) >= 2]
    assert multi, f"{p2_key} has no question that needs two sources; it would not measure Phase 2"


def test_phase2_keys_exercise_every_source_and_a_missing_evidence_case(p2_key):
    qs = load(p2_key)
    covered = {ev.source_of(c) for q in qs for c in q["expected_citations"]}
    assert set(config.SOURCES) <= covered, f"{p2_key}: no expected citation from {sorted(set(config.SOURCES) - covered)}"
    assert any(not q["expected_citations"] for q in qs) or any("not enough evidence" in q["expected_answer"].lower() or
                                                              "unavailable" in q["expected_answer"].lower() for q in qs), \
        f"{p2_key}: no question where the right answer is 'not enough evidence' or 'source unavailable'"


def test_no_expected_answer_cites_an_id_that_is_not_in_expected_citations(p2_key):
    """If the written expected answer names an item, that item must be in expected_citations (or forbidden)."""
    for q in load(p2_key):
        named = set(re.findall(r"\b(?:ATL|NTN|EML)-\d{3}\b", q["expected_answer"]))
        allowed = set(q["expected_citations"]) | set(q.get("forbidden_citations", []))
        assert named <= allowed, f"{shown(p2_key, q, 'expected_answer')}: names {sorted(named - allowed)} that the lists do not mention"


# ---------------------------------------------------------------- the rules themselves (key checks must work on bad keys)
def test_the_reserved_check_would_catch_a_reserved_id_and_phrase():
    raw = json.dumps([{"id": "Q1", "expected_citations": ["ATL-141"], "expected_answer": "the Vendor Onboarding Checklist"}]).lower()
    assert any(r.lower() in raw for r in RESERVED)
    assert any(p in raw for p in RESERVED_TEXT)


def test_reserved_fixtures_file_adds_to_the_hard_coded_ones(tmp_path, monkeypatch):
    f = tmp_path / "reserved.json"
    f.write_text(json.dumps({"ids": ["NTN-210", "EML-305"], "phrases": ["Secret Phrase"], "ignored": 1}))
    monkeypatch.setattr(config, "RESERVED_FIXTURES_PATH", f)
    r = corpus.reserved()
    assert {"ATL-141", "ATL-137", "NTN-210", "EML-305"} <= r["ids"] and "secret phrase" in r["phrases"]
    f.write_text(json.dumps(["NTN-211"]))
    assert "NTN-211" in corpus.reserved()["ids"]
    f.write_text("not json")
    assert corpus.reserved()["ids"] == {"ATL-141", "ATL-137"}      # an unreadable file never weakens the rule
