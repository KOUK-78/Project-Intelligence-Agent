"""Answer-key leakage guard (CONTRACTS.md rules 2, 3 and 4).

The agent must work on questions nobody has seen. Prompts and node logic therefore may not contain
anything lifted from the data or the answer key: real ticket IDs, ticket titles, people's names, or
answer-key wording. Examples in prompts use placeholders (ATL-000, 'label-a', 'term-a').

What is scanned: app/agent.py, app/graph.py, app/nodes/*.py (including planner.py and evidence_check.py) and
app/sources.py when it exists, as raw text (so comments and docstrings count) AND as the string constants the
modules actually expose at runtime (so f-string prompts are checked after formatting).

Phase 2: the rules cover ALL sources. IDs, titles, people's names, email addresses and labels come from the Jira,
Notion and email data (tests/corpus.py reads them without assuming a layout), and answer-key wording comes from
the original and Phase 2 tuning keys. The Phase 2 held-out key is checked too, but a failure NEVER prints its
wording (B and D must not see it before M4): it names the key and question only.
"""
import ast
import importlib
import json
import re
from pathlib import Path

import pytest

from app import config
from tests import corpus

ROOT = Path(__file__).resolve().parents[1]
_EXTRA = [ROOT / "app" / "sources.py"] if (ROOT / "app" / "sources.py").exists() else []
SOURCES = [ROOT / "app" / "agent.py", ROOT / "app" / "graph.py", *_EXTRA, *sorted((ROOT / "app" / "nodes").glob("*.py"))]
PLACEHOLDER_ID = re.compile(r"-00\d$")
SHINGLE_WORDS = 5
MODULES = ["app.agent", "app.graph"] + (["app.sources"] if _EXTRA else []) + [
    f"app.nodes.{p.stem}" for p in sorted((ROOT / "app" / "nodes").glob("*.py")) if p.stem != "__init__"
]


def tickets() -> list[dict]:
    raw = json.loads(Path(config.DATA_PATH).read_text(encoding="utf-8"))
    return raw["tickets"] if isinstance(raw, dict) else raw


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def shingles(text: str, n: int = SHINGLE_WORDS) -> set[tuple[str, ...]]:
    w = words(text)
    return {tuple(w[i : i + n]) for i in range(len(w) - n + 1)}


def source_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in SOURCES}


def runtime_strings() -> dict[str, str]:
    """Every module-level string constant (prompts, hints, regex sources), after f-string formatting."""
    out: dict[str, str] = {}
    for name in MODULES:
        mod = importlib.import_module(name)
        for attr, value in vars(mod).items():
            if isinstance(value, str) and not attr.startswith("__"):
                out[f"{name}.{attr}"] = value
            elif isinstance(value, re.Pattern):
                out[f"{name}.{attr}"] = value.pattern
    return out


def everything() -> dict[str, str]:
    return {**{f"source:{k}": v for k, v in source_texts().items()}, **{f"runtime:{k}": v for k, v in runtime_strings().items()}}


# ---------------------------------------------------------------- B1: leakage

ANY_ID = re.compile(r"\b[A-Z]{2,10}-\d{2,}\b")


def test_the_corpus_finds_the_phase1_data():
    """Guard the guard: if the corpus reader stopped finding Jira facts, every test below would pass vacuously."""
    f = corpus.facts("jira")
    assert len(f["ids"]) == len(tickets()) and f["titles"] and f["names"] and f["labels"]


def test_no_real_item_id_appears_in_prompts_or_node_logic():
    real_ids = corpus.facts()["ids"]
    prefixes = set(config.SOURCE_ID_PREFIXES.values())
    bad = []
    for where, text in everything().items():
        for cid in ANY_ID.findall(text):
            if cid in real_ids or not PLACEHOLDER_ID.search(cid):
                bad.append(f"{where}: {cid}")
    assert not bad, "real or non-placeholder item IDs found (use ATL-000, NTN-000, EML-000):\n" + "\n".join(sorted(set(bad)))
    assert prefixes == {"ATL", "NTN", "EML"}


def test_no_item_title_appears_in_prompts_or_node_logic():
    titles = corpus.facts()["titles"]
    bad = [f"{where}: {title!r}" for where, text in everything().items() for title in titles if title in text.lower()]
    assert not bad, "ticket, page or email titles found:\n" + "\n".join(sorted(set(bad)))


def test_no_person_name_or_address_from_the_data_appears_in_prompts_or_node_logic():
    f = corpus.facts()
    bad = []
    for where, text in everything().items():
        low = text.lower()
        bad += [f"{where}: {n!r}" for n in f["names"] if re.search(rf"\b{re.escape(n)}\b", low)]
        bad += [f"{where}: {a!r}" for a in f["addresses"] if a in low]
    assert not bad, "people's names or email addresses found:\n" + "\n".join(sorted(set(bad)))


def key_shingles_for(include_heldout: bool) -> dict[tuple[str, ...], str]:
    out: dict[tuple[str, ...], str] = {}
    for name, questions in corpus.key_questions(include_heldout).items():
        if not include_heldout and name in corpus.HELDOUT_FILES:
            continue
        for q in questions:
            for field in ("question", "expected_answer", "notes"):
                for sh in shingles(str(q.get(field, ""))):
                    out.setdefault(sh, f"{name}:{q.get('id')}.{field}")
    return out


def test_no_answer_key_wording_appears_in_prompts_or_node_logic():
    key_shingles = key_shingles_for(include_heldout=False)
    assert key_shingles, "no answer key found"
    bad = []
    for where, text in everything().items():
        for sh in shingles(text) & key_shingles.keys():
            bad.append(f"{where}: '{' '.join(sh)}' (from {key_shingles[sh]})")
    assert not bad, f"{SHINGLE_WORDS}-word runs copied from the answer keys:\n" + "\n".join(sorted(set(bad)))


def test_no_heldout_wording_appears_in_prompts_or_node_logic_and_the_failure_never_quotes_it():
    """Reads the held-out keys only to compare. A failure names the file and key, never the words."""
    held = {}
    for name, questions in corpus.key_questions(include_heldout=True).items():
        if name in corpus.HELDOUT_FILES:
            for q in questions:
                for field in ("question", "expected_answer", "notes"):
                    for sh in shingles(str(q.get(field, ""))):
                        held.setdefault(sh, f"{name}:{q.get('id')}.{field}")
    bad = sorted({f"{where} copies wording from {held[sh]} (text withheld)"
                  for where, text in everything().items() for sh in shingles(text) & held.keys()})
    assert not bad, "held-out wording found in agent code:\n" + "\n".join(bad)


def test_the_data_words_in_prompt_examples_are_placeholders():
    # Labels are domain vocabulary: an example label in a prompt would teach the model THIS dataset.
    labels = corpus.facts()["labels"]
    prompts = {k: v for k, v in runtime_strings().items() if re.search(r"(SYSTEM|NUDGE|HINT|PROMPT)", k.split(".")[-1])}
    assert prompts, "no prompt constants found; the scan is looking in the wrong place"
    bad = [f"{k}: {label!r}" for k, v in prompts.items() for label in labels
           if re.search(rf"\b{re.escape(label)}\b", v.lower())]
    assert not bad, "dataset labels used as prompt examples:\n" + "\n".join(sorted(set(bad)))


def test_prompt_constants_exist_for_every_phase2_node_that_has_a_module():
    """A planner or evidence-check module with no prompt constant would escape the label scan above."""
    for stem in ("planner", "evidence_check"):
        path = ROOT / "app" / "nodes" / f"{stem}.py"
        if not path.exists():
            continue
        mod = importlib.import_module(f"app.nodes.{stem}")
        assert any(isinstance(v, str) and re.search(r"(SYSTEM|PROMPT|NUDGE|HINT)", k) for k, v in vars(mod).items()), \
            f"app/nodes/{stem}.py has no *_SYSTEM / *_PROMPT constant; name its prompt so the hygiene scan sees it"


# ---------------------------------------------------------------- rule 2 and rule 4

def test_agent_code_never_reads_the_heldout_key():
    offenders = [name for name, text in source_texts().items() if re.search(r"heldout|held-out|held_out", text, re.I)]
    assert not offenders, f"held-out key referenced in agent code (it is measurement-only): {offenders}"


def test_no_tunables_outside_config():
    """Module-level numeric constants and numeric slice limits belong in app/config.py."""
    bad = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, (int, float)) and not isinstance(node.value.value, bool):
                bad.append(f"{path.name}:{node.lineno} module constant")
        for node in ast.walk(tree):
            if isinstance(node, ast.Slice):
                for part in (node.lower, node.upper):
                    if isinstance(part, ast.Constant) and isinstance(part.value, int) and part.value not in (0, 1):
                        bad.append(f"{path.name}:{node.lineno} slice limit {part.value}")
    assert not bad, "move these into app/config.py:\n" + "\n".join(bad)
    assert not (ROOT / "app" / "nodes" / "settings.py").exists()


# ---------------------------------------------------------------- the guard itself must work

@pytest.mark.parametrize("leak", ["ATL-101", "ATL-999"])
def test_the_id_check_would_catch_a_leak(leak):
    real_ids = {t["id"] for t in tickets()} | {"ATL-999"}
    found = re.findall(r"\b[A-Z]{2,10}-\d{2,}\b", f"e.g. search '{leak}'")
    assert any(c in real_ids or not PLACEHOLDER_ID.search(c) for c in found)
    assert not any(c in real_ids or not PLACEHOLDER_ID.search(c)
                   for c in re.findall(r"\b[A-Z]{2,10}-\d{2,}\b", "e.g. search 'ATL-000'"))


def test_the_shingle_check_would_catch_copied_wording():
    key_text = "It holds up the checkout card payment screen and the refund workflow"
    leaked = "Note: it holds up the checkout card payment screen."
    assert shingles(key_text) & shingles(leaked)
    assert not shingles(key_text) & shingles("Look facts up with tools; never answer from memory.")


# ---------------------------------------------------------------- B2: heuristics are documented and switchable

def test_every_heuristic_flag_has_a_general_or_specific_note_in_code():
    code = "\n".join(source_texts().values())
    for name in config.HEURISTIC_DEFAULTS:
        assert re.search(rf"HEURISTIC {name} \[(general|specific-risk)", code), f"no '# HEURISTIC {name} [general|specific-risk]' note"


def test_heuristic_env_overrides_and_rejects_unknown_names():
    on = config.resolve_heuristics({"AGENT_HEURISTICS_ON": "ref_round, candidate_round", "AGENT_HEURISTICS_OFF": "filter_guard"})
    assert on["ref_round"] and on["candidate_round"] and not on["filter_guard"]
    assert config.resolve_heuristics({}) == config.HEURISTIC_DEFAULTS
    with pytest.raises(ValueError):
        config.resolve_heuristics({"AGENT_HEURISTICS_ON": "nope"})
