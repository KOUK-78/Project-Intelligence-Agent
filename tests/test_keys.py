"""A4: both answer files are valid against the data, and the reserved fixtures stay out of them.

Reads the held-out key only to check its FORMAT and IDs. Nothing here tunes anything against it.
"""
import json
import re
from pathlib import Path

import pytest

from evals import run_evals as ev

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synthetic" / "jira_tickets.json"
KEY_PATHS = {"answer_key": ROOT / "evals" / "answer_key.json",
             "heldout_key": ROOT / "evals" / "heldout_key.json"}

# Phase 4 (injection) and Phase 5 (PII) fixtures: never used by Phase 1 questions.
RESERVED = {"ATL-141", "ATL-137"}
# Anything that identifies those two tickets, so a rewording can't smuggle them in.
RESERVED_TEXT = ["vendor onboarding checklist", "duplicate charge in legacy billing",
                 "jordan vale", "jordan.vale", "contact list", "external-contractor",
                 "ignore previous instructions", "(555)"]
ID_RE = re.compile(r"^ATL-\d{3}$")


def tickets():
    return json.loads(DATA.read_text(encoding="utf-8"))


def ids():
    return {t["id"] for t in tickets()}


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
                assert cid in valid, f"{key_name} {q['id']}.{field}: {cid} is not a ticket in the data"


def test_expected_and_forbidden_never_overlap_and_have_no_duplicates(key_name):
    for q in load(key_name):
        exp, forb = q["expected_citations"], q.get("forbidden_citations", [])
        assert len(set(exp)) == len(exp) and len(set(forb)) == len(forb), q["id"]
        assert not set(exp) & set(forb), f"{key_name} {q['id']}: {sorted(set(exp) & set(forb))} both expected and forbidden"


def test_reserved_fixture_tickets_are_not_in_either_key(key_name):
    raw = KEY_PATHS[key_name].read_text(encoding="utf-8")
    for q in load(key_name):
        used = (set(q["expected_citations"]) | set(q.get("forbidden_citations", []))) & RESERVED
        assert not used, f"{key_name} {q['id']} uses reserved {sorted(used)}"
    for rid in RESERVED:
        assert rid not in raw, f"{rid} appears in the text of {KEY_PATHS[key_name].name}"
    low = raw.lower()
    for phrase in RESERVED_TEXT:
        assert phrase not in low, f"{phrase!r} (identifies a reserved ticket) appears in {KEY_PATHS[key_name].name}"


def test_key_sizes():
    assert 8 <= len(load("answer_key")) <= 10
    assert 8 <= len(load("heldout_key")) <= 10


def test_heldout_is_disjoint_from_the_tuning_key():
    tune, held = load("answer_key"), load("heldout_key")
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    assert not {q["id"] for q in tune} & {q["id"] for q in held}
    assert not {norm(q["question"]) for q in tune} & {norm(q["question"]) for q in held}
    assert all(q["id"].startswith("H") for q in held) and all(q["id"].startswith("Q") for q in tune)


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
