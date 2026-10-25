"""The frozen key hash in CONTRACTS.md section 7 must match the committed tuning key (changing the key needs a new hash)."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phase2_key_matches_its_frozen_hash():
    text = (ROOT / "CONTRACTS.md").read_text(encoding="utf-8")
    row = re.search(r"\| `evals/phase2_key\.json` \| `([0-9a-f]{64})`", text)
    assert row, "CONTRACTS.md section 7 has no frozen hash for evals/phase2_key.json"
    actual = hashlib.sha256((ROOT / "evals" / "phase2_key.json").read_bytes()).hexdigest()
    assert actual == row.group(1), ("evals/phase2_key.json changed after the freeze: all four must approve a new hash "
                                    "(or the file has CRLF line endings: check .gitattributes)")


def test_key_files_are_pinned_to_lf():
    assert "evals/*.json text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
