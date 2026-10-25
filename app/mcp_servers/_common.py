"""Helpers shared by the Notion and email servers. OWNER: D.

Rules every server here follows: never write to stdout (it carries the MCP protocol), read-only,
bad input or unreadable data returns an {"error": ...} object and never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Generic English filler only. Nothing about the data belongs here.
STOPWORDS = {"the", "a", "an", "of", "is", "are", "was", "were", "what", "which", "why", "how", "for",
             "to", "in", "on", "and", "or", "about", "with", "did", "does", "do"}


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format=f"%(asctime)s %(levelname)s {name}: %(message)s")
    return logging.getLogger(name)


def error(log: logging.Logger, code: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"error": code, **extra, "message": message}
    log.warning("returning error: %s", out)
    return out


def data_path(env_var: str, default: Path) -> Path:
    env = os.environ.get(env_var)
    p = Path(env or default)
    return p if p.is_absolute() else REPO_ROOT / p


def load_records(path: Path, key: str) -> list[dict[str, Any]]:
    """Read the data file on every call (tiny file, no stale state). Accepts a list or {key: list}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get(key, []) if isinstance(raw, dict) else raw
    return [r for r in items if isinstance(r, dict) and isinstance(r.get("id"), str)]


def tokens(keyword: str) -> list[str]:
    """'*' means no restriction. Otherwise every remaining token must appear."""
    if keyword.strip() == "*":
        return []
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]*", keyword.lower()) if w not in STOPWORDS]


# ---- ranked keyword search (Notion and email). A multi-word query used to need EVERY word in one record, so a
# question-shaped query matched nothing. Now records are ranked by how many query words they contain (a hit in the
# title or subject counts triple) and the newest wins ties. Word forms are matched by a light stem comparison.
_SUFFIXES = ("ations", "ation", "ments", "ment", "ities", "ity", "ings", "ing", "edly", "ed", "ies", "es", "ly", "s")
TITLE_WEIGHT = 3
MIN_STEM = 5  # two stems match when equal, or when the shorter (>= MIN_STEM letters) is a prefix of the other


def stem(word: str) -> str:
    if any(ch.isdigit() for ch in word) or "-" in word:  # IDs, dates, versions stay exact
        return word
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)] + ("y" if suf == "ies" else "")
    return word


def _term_hits(term: str, doc_stems: set[str]) -> bool:
    if term in doc_stems:
        return True
    if len(term) < MIN_STEM or not term.isalpha():  # IDs, dates and versions never match by prefix
        return False
    return any(d.isalpha() and len(d) >= MIN_STEM and (d.startswith(term) or term.startswith(d)) for d in doc_stems)


def stems_of(text: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z0-9][a-z0-9\-\.]*", text.lower())}


def relevance(terms: list[str], title: str, body: str) -> int:
    """0 = no query word found; otherwise title hits count TITLE_WEIGHT each, body hits 1 each."""
    if not terms:
        return 1  # '*' (or only filler words): every record qualifies, ordered by date
    t_stems, b_stems = stems_of(title), stems_of(body)
    return sum(TITLE_WEIGHT if _term_hits(t, t_stems) else 1 for t in terms if _term_hits(t, t_stems | b_stems))


def query_terms(keyword: str) -> list[str]:
    return list(dict.fromkeys(stem(t) for t in tokens(keyword)))


def snippet(text: str, toks: list[str], width: int = 160) -> str:
    """A window of `text` around the first token hit (start of text when there is none)."""
    low = text.lower()
    hits = [i for i in (low.find(t) for t in toks) if i >= 0]
    start = max(min(hits) - width // 4, 0) if hits else 0
    out = text[start:start + width].strip()
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


def cap(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit].rstrip() + "…", True)


def same_id(record_id: str, wanted: str) -> bool:
    """Case-insensitive; a bare number matches the numeric part of the ID ('1' == 'XXX-001')."""
    w = wanted.strip().upper()
    if w.isdigit():
        tail = record_id.rsplit("-", 1)[-1]
        return tail.isdigit() and int(tail) == int(w)
    return record_id.upper() == w
