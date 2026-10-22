"""Answer-key leakage guard (CONTRACTS.md rules 2, 3 and 4).

The agent must work on questions nobody has seen. Prompts and node logic therefore may not contain
anything lifted from the data or the answer key: real ticket IDs, ticket titles, people's names, or
answer-key wording. Examples in prompts use placeholders (ATL-000, 'label-a', 'term-a').

What is scanned: app/agent.py, app/graph.py and app/nodes/*.py, as raw text (so comments and
docstrings count) AND as the string constants the modules actually expose at runtime (so f-string
prompts are checked after formatting).
"""
import ast
import importlib
import json
import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "app" / "agent.py", ROOT / "app" / "graph.py", *sorted((ROOT / "app" / "nodes").glob("*.py"))]
PLACEHOLDER_ID = re.compile(r"-00\d$")
SHINGLE_WORDS = 5
MODULES = ["app.agent", "app.graph"] + [
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

def test_no_real_ticket_id_appears_in_prompts_or_node_logic():
    real_ids = {t["id"] for t in tickets()}
    bad = []
    for where, text in everything().items():
        for cid in re.findall(r"\b[A-Z]{2,10}-\d{2,}\b", text):
            if cid in real_ids or not PLACEHOLDER_ID.search(cid):
                bad.append(f"{where}: {cid}")
    assert not bad, "real or non-placeholder ticket IDs found (use ATL-000):\n" + "\n".join(sorted(set(bad)))


def test_no_ticket_title_appears_in_prompts_or_node_logic():
    titles = {t["title"].lower() for t in tickets() if len(t["title"]) >= 10}
    bad = [f"{where}: {title!r}" for where, text in everything().items() for title in titles if title in text.lower()]
    assert not bad, "ticket titles found:\n" + "\n".join(sorted(set(bad)))


def test_no_person_name_from_the_data_appears_in_prompts_or_node_logic():
    names = set()
    for t in tickets():
        for full in [t["assignee"], *[c["author"] for c in t["comments"]]]:
            names.add(full.lower())
            names.update(part for part in full.lower().split() if len(part) >= 5)
    bad = [f"{where}: {n!r}" for where, text in everything().items() for n in names
           if re.search(rf"\b{re.escape(n)}\b", text.lower())]
    assert not bad, "people's names found:\n" + "\n".join(sorted(set(bad)))


def test_no_answer_key_wording_appears_in_prompts_or_node_logic():
    key = json.loads(Path(config.ANSWER_KEY_PATH).read_text(encoding="utf-8"))
    key_shingles: dict[tuple[str, ...], str] = {}
    for q in key:
        for field in ("question", "expected_answer", "notes"):
            for sh in shingles(q[field]):
                key_shingles.setdefault(sh, f"{q['id']}.{field}")
    bad = []
    for where, text in everything().items():
        for sh in shingles(text) & key_shingles.keys():
            bad.append(f"{where}: '{' '.join(sh)}' (from {key_shingles[sh]})")
    assert not bad, f"{SHINGLE_WORDS}-word runs copied from the answer key:\n" + "\n".join(sorted(set(bad)))


def test_the_data_words_in_prompt_examples_are_placeholders():
    # Labels are domain vocabulary: an example label in a prompt would teach the model THIS dataset.
    labels = {label.lower() for t in tickets() for label in t["labels"] if len(label) >= 5}
    prompts = {k: v for k, v in runtime_strings().items() if re.search(r"(SYSTEM|NUDGE|HINT)", k.split(".")[-1])}
    assert prompts, "no prompt constants found; the scan is looking in the wrong place"
    bad = [f"{k}: {label!r}" for k, v in prompts.items() for label in labels
           if re.search(rf"\b{re.escape(label)}\b", v.lower())]
    assert not bad, "dataset labels used as prompt examples:\n" + "\n".join(sorted(set(bad)))


# ---------------------------------------------------------------- rule 2 and rule 4

def test_agent_code_never_reads_the_heldout_key():
    offenders = [name for name, text in source_texts().items() if re.search(r"heldout|held-out", text, re.I)]
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
