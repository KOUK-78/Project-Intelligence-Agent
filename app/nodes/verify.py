"""Citation verification in plain Python (no model). OWNER: B.

Seed of the Phase 5 citation verifier. A cited ID survives only if it
  1. exists in the data (when the data file is readable), and
  2. is grounded in THIS run's tool output: returned as a ticket record, or named inside
     the text of a ticket that was returned (e.g. "waiting on ATL-112").
Anything else is dropped from `citations` and flagged in the answer text.
`citations` is derived from the answer text, so it can never list an ID the answer doesn't use.
"""
import json
import re
from pathlib import Path
from typing import Any

from app import config

ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


_BARE_ID_RE = re.compile(r"(?<![\[\w-])([A-Z][A-Z0-9]{1,9}-\d+)(?![\]\w-])")


def bracket_bare_ids(answer: str) -> str:
    """The model often writes 'ATL-112' instead of '[ATL-112]'. Enforce the format in code."""
    return _BARE_ID_RE.sub(r"[\1]", answer)


def load_known_ids(path: str | Path | None = None) -> set[str] | None:
    """All ticket IDs in the data file, or None if it can't be read (stub phase)."""
    try:
        raw = json.loads(Path(path or config.DATA_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = raw.get("tickets", []) if isinstance(raw, dict) else raw
    ids = {t["id"] for t in items if isinstance(t, dict) and isinstance(t.get("id"), str)}
    return ids or None


def evidence_ids(evidence: list[dict[str, Any]]) -> set[str]:
    """Ticket IDs present in successful tool results (not_found/error payloads don't count)."""
    found: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "error" in x:
                return
            v = x.get("id")
            if isinstance(v, str) and ID_RE.fullmatch(v):
                found.add(v)
            for child in x.values():
                walk(child)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    for e in evidence:
        if e.get("ok"):
            walk(e.get("data"))
    return found


def mentioned_ids(evidence: list[dict[str, Any]]) -> list[str]:
    """IDs named inside the text of successful tool results (descriptions, comments), in order."""
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "error" in x:
                return
            for key, value in x.items():
                if key != "id":
                    walk(value)
        elif isinstance(x, list):
            for item in x:
                walk(item)
        elif isinstance(x, str):
            for cid in ID_RE.findall(x):
                if cid not in found:
                    found.append(cid)

    for e in evidence:
        if e.get("ok"):
            walk(e.get("data"))
    return found


def verify_citations(
    answer: str,
    citations: list[str],
    *,
    known_ids: set[str] | None,
    ev_ids: set[str],
    mentioned: set[str] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Returns (answer, verified_citations, warnings). Citations come from the answer text."""
    warnings: list[str] = []
    mentioned = mentioned or set()
    in_answer: list[str] = []
    for cid in ID_RE.findall(answer):
        if cid not in in_answer:
            in_answer.append(cid)
    for raw in citations:
        cid = str(raw).strip().strip("[]").upper()
        if cid and cid not in in_answer:
            warnings.append(f"citation_not_in_answer:{cid}")

    if known_ids is None:
        warnings.append("data_file_unavailable: citations checked against tool evidence only")

    valid: list[str] = []
    for cid in in_answer:
        if known_ids is not None and cid not in known_ids:
            warnings.append(f"unknown_ticket_id:{cid}")
            answer = answer.replace(f"[{cid}]", f"[unverified: {cid}]")
        elif cid in ev_ids:
            valid.append(cid)
        elif cid in mentioned:
            warnings.append(f"cited_from_mention:{cid}")  # real, named in a returned ticket, not itself read
            valid.append(cid)
        else:
            warnings.append(f"not_in_evidence:{cid}")
            answer = answer.replace(f"[{cid}]", f"[unverified: {cid}]")
    return answer, valid, warnings
