"""Shared reader for the rule-policing tests (C6). OWNER: C.

Loads every source's synthetic data (Jira tickets, Notion pages, emails) WITHOUT assuming a file layout: A owns
the Notion and email schemas, so this walks the JSON and picks fields by name. A missing data file simply
contributes nothing, so the tests keep working before A's data lands and tighten automatically after.

Field names it understands (add to the tuples below if A names one differently):
"""
import json
import re
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[1]
TITLE_FIELDS = ("title", "subject", "heading", "name_of_page")
PERSON_FIELDS = ("assignee", "author", "from", "sender", "to", "cc", "bcc", "recipients", "owner", "created_by",
                 "last_edited_by", "attendees", "participants", "reporter")
LABEL_FIELDS = ("labels", "tags")
ID_RE = re.compile(r"^[A-Z]{2,10}-\d+$")
KEY_FILES = {"answer_key": "answer_key.json", "phase2_key": "phase2_key.json"}
HELDOUT_FILES = {"heldout_key": "heldout_key.json", "phase2_heldout_key": "phase2_heldout_key.json"}
EMAIL_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def walk(node):
    """Every dict in a JSON value, depth first."""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def source_data() -> dict[str, object]:
    """{source: parsed JSON} for the sources whose data file exists."""
    out = {}
    for source in config.SOURCES:
        data = load_json(config.DATA_PATHS[source])
        if data is not None:
            out[source] = data
    return out


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for k in ("name", "email", "address", "display_name"):
            if isinstance(value.get(k), str):
                yield value[k]


def facts(source: str | None = None) -> dict[str, set[str]]:
    """Item IDs, titles, person names, email addresses and labels found in the data (lower-cased except IDs)."""
    ids, titles, people, labels = set(), set(), set(), set()
    for src, data in source_data().items():
        if source and src != source:
            continue
        prefix = config.SOURCE_ID_PREFIXES[src]
        for d in walk(data):
            if isinstance(d.get("id"), str) and ID_RE.match(d["id"]) and d["id"].startswith(prefix + "-"):
                ids.add(d["id"])
            for f in TITLE_FIELDS:
                if isinstance(d.get(f), str) and len(d[f]) >= 10:
                    titles.add(d[f].lower())
            for f in PERSON_FIELDS:
                for s in _strings(d.get(f)):
                    people.add(s.lower())
            for f in LABEL_FIELDS:
                for s in _strings(d.get(f)):
                    if len(s) >= 5:
                        labels.add(s.lower())
    names = set()
    addresses = set()
    for p in people:
        for a in EMAIL_ADDR.findall(p):
            addresses.add(a)
        text = EMAIL_ADDR.sub(" ", p)
        names.add(" ".join(text.split()))
        names.update(part for part in re.findall(r"[a-z]+", text) if len(part) >= 5)
    names.discard("")
    # The project name is allowed in prompts (config.PROJECT_NAME); a mailing list called "<Project> Team" is not a person.
    names -= {w.lower() for w in re.findall(r"[A-Za-z]+", config.PROJECT_NAME)}
    return {"ids": ids, "titles": titles, "names": names, "addresses": addresses, "labels": labels}


def key_questions(include_heldout: bool = False) -> dict[str, list[dict]]:
    """{key name: questions} for keys that exist. Held-out keys only when asked, and only for checks that never
    print their wording."""
    files = dict(KEY_FILES)
    if include_heldout:
        files |= HELDOUT_FILES
    out = {}
    for name, fname in files.items():
        data = load_json(ROOT / "evals" / fname)
        if isinstance(data, list):
            out[name] = data
    return out


def reserved() -> dict[str, set[str]]:
    """Reserved fixtures (injection, PII). Phase 1 hard-codes two; A's data/reserved_fixtures.json adds the rest.

    Accepted shapes: ["NTN-210", ...] or {"ids": [...], "phrases": [...]} (extra keys are ignored).
    """
    ids = {"ATL-141", "ATL-137"}
    phrases = {"vendor onboarding checklist", "duplicate charge in legacy billing", "jordan vale", "jordan.vale",
               "contact list", "external-contractor", "ignore previous instructions", "(555)"}
    data = load_json(config.RESERVED_FIXTURES_PATH)
    if isinstance(data, list):
        ids |= {x for x in data if isinstance(x, str)}
    elif isinstance(data, dict):
        ids |= {x for x in data.get("ids", []) if isinstance(x, str)}
        phrases |= {x.lower() for x in data.get("phrases", []) if isinstance(x, str)}
    return {"ids": ids, "phrases": phrases}
