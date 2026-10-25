"""Phase 2 A4: both Phase 2 answer files are valid against all three data sources, use the optional
fields correctly, and keep every reserved fixture out. Reads the held-out key only to check FORMAT and IDs."""
import json
import os
import re
from pathlib import Path

import pytest

from evals import run_evals as ev

ROOT = Path(__file__).resolve().parents[1]
SYN = ROOT / "data" / "synthetic"
# The held-out key is kept outside the repo (CONTRACTS.md 7). Its tests run only where PHASE2_HELDOUT_KEY_PATH points at it.
_HELD = Path(os.getenv("PHASE2_HELDOUT_KEY_PATH") or ROOT / "evals" / "phase2_heldout_key.json")
KEY_PATHS = {"phase2_key": ROOT / "evals" / "phase2_key.json",
             **({"phase2_heldout_key": _HELD} if _HELD.exists() else {})}
SOURCE_OF = {"ATL": "jira", "NTN": "notion", "EML": "email"}
ID_RE = re.compile(r"^(ATL|NTN|EML)-\d{3}$")

RESERVED = {"ATL-141", "ATL-137", "NTN-120", "EML-136"}
RESERVED_TEXT = ["vendor onboarding checklist", "duplicate charge in legacy billing", "jordan vale", "jordan.vale",
                 "contact list", "external-contractor", "ignore previous instructions", "(555)",
                 "riley hart", "riley.hart", "beta partner contact", "sandbox onboarding checklist",
                 "assistant instruction", "paygate-support", "audit@", "disregard"]


def all_ids():
    out = set()
    for f in ("jira_tickets.json", "notion_pages.json", "email_messages.json"):
        out |= {x["id"] for x in json.loads((SYN / f).read_text(encoding="utf-8"))}
    return out


def load(name):
    if name not in KEY_PATHS:
        pytest.skip(f"{name} is not available here (kept outside the repo)")
    return json.loads(KEY_PATHS[name].read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(KEY_PATHS))
def key_name(request):
    return request.param


def test_key_loads_with_the_eval_runners_own_validator(key_name):
    assert ev.load_key(KEY_PATHS[key_name])


def test_key_sizes():
    assert 12 <= len(load("phase2_key")) <= 15
    assert 8 <= len(load("phase2_heldout_key")) <= 12


def test_every_expected_and_forbidden_id_exists_in_the_data(key_name):
    valid = all_ids()
    for q in load(key_name):
        for field in ("expected_citations", "forbidden_citations"):
            for cid in q.get(field, []):
                assert ID_RE.match(cid) and cid in valid, f"{key_name} {q['id']}.{field}: {cid}"


def test_expected_and_forbidden_never_overlap_and_have_no_duplicates(key_name):
    for q in load(key_name):
        exp, forb = q["expected_citations"], q.get("forbidden_citations", [])
        assert len(set(exp)) == len(exp) and len(set(forb)) == len(forb), q["id"]
        assert not set(exp) & set(forb), q["id"]


def test_reserved_fixtures_are_not_in_either_key(key_name):
    raw = KEY_PATHS[key_name].read_text(encoding="utf-8")
    for rid in RESERVED:
        assert rid not in raw, f"{rid} appears in {KEY_PATHS[key_name].name}"
    low = raw.lower()
    for phrase in RESERVED_TEXT:
        assert phrase not in low, f"{phrase!r} appears in {KEY_PATHS[key_name].name}"


def test_sources_required_is_a_valid_list_and_is_covered_by_expected_citations(key_name):
    for q in load(key_name):
        if "sources_required" not in q:
            continue
        src = q["sources_required"]
        assert isinstance(src, list) and src and set(src) <= {"jira", "notion", "email"}, q["id"]
        assert len(set(src)) == len(src), q["id"]
        covered = {SOURCE_OF[c[:3]] for c in q["expected_citations"]}
        assert set(src) <= covered, f"{q['id']}: sources_required {src} not backed by expected_citations"


def test_must_mention_is_a_list_of_strings_that_the_expected_answer_itself_contains(key_name):
    for q in load(key_name):
        if "must_mention" not in q:
            continue
        mm = q["must_mention"]
        assert isinstance(mm, list) and mm and all(isinstance(s, str) and s for s in mm), q["id"]
        for s in mm:
            assert s.lower() in q["expected_answer"].lower(), f"{q['id']}: {s!r} not in expected_answer"


def test_the_questions_cover_the_required_shapes():
    key = load("phase2_key")
    shapes = [set(q.get("sources_required", [])) for q in key]
    assert any(s == {"notion"} for s in shapes), "need a single-source Notion lookup"
    assert any(s == {"email"} for s in shapes), "need a single-source email lookup"
    assert any(s == {"jira", "email"} for s in shapes), "need a Jira + email join"
    assert any(s == {"jira", "notion", "email"} for s in shapes), "need a three-source question"
    assert any(s == {"jira", "notion"} for s in shapes), "need a Notion vs Jira conflict"
    assert any(not q["expected_citations"] for q in key), "need a no-data question"


def test_question_ids_and_wording_do_not_overlap_between_the_two_keys():
    a, b = load("phase2_key"), load("phase2_heldout_key")
    assert not {q["id"] for q in a} & {q["id"] for q in b}
    assert not {q["question"] for q in a} & {q["question"] for q in b}


def test_no_data_questions_ask_about_topics_absent_from_every_source():
    blob = "".join((SYN / f).read_text(encoding="utf-8").lower()
                   for f in ("jira_tickets.json", "notion_pages.json", "email_messages.json"))
    for topic in ("zendesk", "loyalty"):
        assert topic not in blob
