"""C6: CONTRACTS.md and app/config.py must agree, and every source file must have an owner.

These tests police the written contract itself, so a Day 0 decision cannot drift from the code unnoticed.
"""
import re
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = (ROOT / "CONTRACTS.md").read_text(encoding="utf-8")


def section(number: int) -> str:
    """Text of one '## N) ...' section of CONTRACTS.md."""
    m = re.search(rf"^## {number}\) .*?(?=^## |\Z)", CONTRACTS, re.S | re.M)
    assert m, f"CONTRACTS.md has no section {number}"
    return m.group(0)


# ---------------------------------------------------------------- config keys named by the contract
def test_every_config_key_named_in_section_6_exists():
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", section(6)))
    assert {"MODELS", "THINKING", "MAX_AGENT_STEPS", "MAX_TOTAL_STEPS", "MAX_SUBQUESTIONS", "PLANNER_ENABLED"} <= named
    missing = sorted(n for n in named if not hasattr(config, n))
    assert not missing, f"CONTRACTS.md section 6 names config keys that do not exist: {missing}"


def test_phase2_jobs_have_a_model_and_a_thinking_flag():
    assert set(config.MODELS) >= {"agent", "synthesizer", "planner", "evidence_check", "rewrite", "judge", "embedding"}
    for job in ("agent", "synthesizer", "planner", "evidence_check", "rewrite"):
        assert isinstance(config.THINKING[job], bool), job


def test_step_caps_are_consistent():
    assert config.MAX_AGENT_STEPS == 5                      # Phase 1 value; now the per-sub-question cap
    assert config.MAX_SUBQUESTIONS >= 1
    assert config.MAX_TOTAL_STEPS >= config.MAX_AGENT_STEPS  # one sub-question must be able to use its cap


def test_planner_switch_is_a_bool_and_off_unless_the_environment_says_otherwise():
    import os
    if "PLANNER_ENABLED" not in os.environ:
        assert config.PLANNER_ENABLED is False       # Phase 1 behaviour is the default until recorded results justify it
    assert isinstance(config.PLANNER_ENABLED, bool)
    assert config.MAX_REWRITES_PER_SUBQUESTION >= 0


# ---------------------------------------------------------------- ID prefixes: doc and code agree
def test_source_registry_matches_the_contract_table():
    assert set(config.SOURCE_ID_PREFIXES) == set(config.SOURCES)
    assert len(set(config.SOURCE_ID_PREFIXES.values())) == len(config.SOURCES), "prefixes must be unique"
    rows = re.findall(r"^\| (\w+) \| `([A-Z]+)` \|", section(10), re.M)
    documented = {name.lower(): prefix for name, prefix in rows}
    assert documented == config.SOURCE_ID_PREFIXES


def test_every_source_has_a_data_path():
    assert set(config.DATA_PATHS) == set(config.SOURCES)


# ---------------------------------------------------------------- amendments and ownership
def test_all_eight_day_0_decisions_are_recorded():
    table = re.search(r"^## 12\) .*?(?=^\*\*Sign-off|\Z)", CONTRACTS, re.S | re.M).group(0)
    assert [int(n) for n in re.findall(r"^\| (\d+) \|", table, re.M)] == list(range(1, 9))


def test_signoff_names_all_four_owners():
    block = CONTRACTS.split("**Sign-off**")[1].split("## Ownership")[0]
    assert re.findall(r"- \[[ x]\] ([ABCD]) ", block) == ["A", "B", "C", "D"]


def owner_text() -> str:
    return CONTRACTS.split("## Ownership")[1]


def python_files() -> list[Path]:
    files = [p for d in ("app", "evals") for p in (ROOT / d).rglob("*.py")]
    return sorted(p for p in files if "__pycache__" not in p.parts and not p.name.startswith("__"))


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/"))
def test_every_python_file_has_an_owner(path):
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    text = owner_text()
    covered = (
        f"`{rel}`" in text
        or (rel.startswith("app/mcp_servers/") and "`app/mcp_servers/`" in text)
        or (rel.startswith("app/nodes/") and (f"`app/nodes/{path.name}`" in text or re.search(rf"`{re.escape(path.name)}`", text)))
    )
    assert covered, f"{rel} is not named in the CONTRACTS.md ownership table"


def test_the_four_owners_each_own_something():
    rows = re.findall(r"^\| ([ABCD]) \| (.+) \|$", owner_text(), re.M)
    assert [r[0] for r in rows] == ["A", "B", "C", "D"]
    assert all(r[1].strip() for r in rows)


# ---------------------------------------------------------------- README stays true to the code and the recorded results
README = (ROOT / "README.md").read_text(encoding="utf-8")


def runner_flags() -> set[str]:
    """Every option the runner defines, read from the command itself. Scraping --help is layout-dependent: typer
    draws it as a rich box that truncates or wraps option names to the terminal width, which differs in CI."""
    import typer.main

    from evals import run_evals

    cmd = typer.main.get_command(run_evals.cli)
    return {opt for param in cmd.params for opt in (*param.opts, *param.secondary_opts)} | {"--help"}


def test_every_runner_flag_the_readme_mentions_exists():
    mentioned = set(re.findall(r"`(--[a-z][a-z0-9-]+)", README + (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")))
    mentioned |= set(re.findall(r"evals\.run_evals [^\n`]*?(--[a-z][a-z0-9-]+)", README))
    ignored = {"--stub", "--no-floor"}  # --stub is also an app CLI flag; --no-floor belongs to evals.compare
    missing = sorted(mentioned - ignored - runner_flags())
    assert not missing, f"README/SETUP mention runner flags that do not exist: {missing}"


def test_the_flag_reader_finds_the_flags_the_docs_rely_on():
    """Guard the guard: if the reader returned an empty set the test above would only fail on typos."""
    assert {"--phase2", "--planner", "--no-planner", "--min-pass-rate", "--no-grade", "--require-trace",
            "--heldout", "--auto", "--repeat"} <= runner_flags()


def test_compare_flag_the_docs_mention_exists():
    import typer.main

    from evals import compare

    assert "--no-floor" in {o for p in typer.main.get_command(compare.cli).params for o in p.opts}


def test_readme_phase1_numbers_match_the_recorded_result_files():
    import json

    for name, runs in (("phase1_baseline.json", "23 of 30 runs"), ("phase1_heldout.json", "18 of 30 runs")):
        score = json.loads((ROOT / "evals" / "results" / name).read_text(encoding="utf-8"))["score"]
        assert f"{score['runs_passed']} of {score['runs_total']} runs" == runs
        assert runs in README, f"README no longer states {runs} for {name}"
    assert "73% to 80%" in README


def test_readme_regression_floors_match_config():
    for key, (got, total) in config.REGRESSION_FLOORS.items():
        assert f"{got} of {total}" in README, f"README does not state the {key} floor {got} of {total}"


def test_readme_claims_no_phase2_result_that_is_not_recorded():
    """A number may enter the Phase 2 table only together with its result file."""
    results = ROOT / "evals" / "results"
    recorded = {p.name for p in results.glob("phase2_*.json") if ".stub." not in p.name}
    table = README.split("### Flat versus planner")[1].split("### Phase 1 regression")[0]
    for name in ("phase2_flat.json", "phase2_planner.json", "phase2_heldout.json", "phase2_heldout_flat.json"):
        row = next(line for line in table.splitlines() if f"`{name}`" in line)
        has_number = bool(re.search(r"\d+ of \d+|\d+\.\ds|\d{3,}", row.split(f"`{name}`")[1]))
        assert has_number == (name in recorded), f"{name}: README row and recorded file disagree (number={has_number})"
