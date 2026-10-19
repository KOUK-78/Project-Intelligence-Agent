"""Citation verification in plain Python (no model). OWNER: B.

Seed of the Phase 5 citation verifier. A cited ID survives only if it
  1. exists in the data (when the data file is readable), and
  2. was actually returned by a tool in THIS run (answers come from evidence only).
Anything else is dropped from `citations` and flagged in the answer text.
"""
import json
import re
from pathlib import Path
from typing import Any

from app import config

ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


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


def verify_citations(
    answer: str,
    citations: list[str],
    *,
    known_ids: set[str] | None,
    ev_ids: set[str],
) -> tuple[str, list[str], list[str]]:
    """Returns (answer, verified_citations, warnings)."""
    warnings: list[str] = []
    claimed: list[str] = []
    for raw in [*citations, *ID_RE.findall(answer)]:
        cid = str(raw).strip().strip("[]").upper()
        if cid and cid not in claimed:
            claimed.append(cid)

    if known_ids is None:
        warnings.append("data_file_unavailable: citations checked against tool evidence only")

    valid: list[str] = []
    for cid in claimed:
        if not ID_RE.fullmatch(cid):
            warnings.append(f"malformed_citation:{cid}")
        elif known_ids is not None and cid not in known_ids:
            warnings.append(f"unknown_ticket_id:{cid}")
            answer = answer.replace(f"[{cid}]", f"[unverified: {cid}]")
        elif cid not in ev_ids:
            warnings.append(f"not_in_evidence:{cid}")
            answer = answer.replace(f"[{cid}]", f"[unverified: {cid}]")
        else:
            valid.append(cid)
    return answer, valid, warnings
