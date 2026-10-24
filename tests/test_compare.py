"""C3: evals/compare.py prints per-question deltas and exits non-zero below the Phase 1 regression floor."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app import config
from evals import compare as cmp

ROOT = Path(__file__).resolve().parents[1]
ENV = {"machine": {"node": "box-1", "platform": "x", "python": "3.12"}, "faults": {},
       "model_fingerprint": {"name": "m", "digest": "abc", "size": 1}}


def rf(passes, key="answer_key.json", runs=3, repeat=3, **extra):
    """A minimal result file. passes = {question id: runs passed}."""
    per_q = [{"id": qid, "runs": runs, "passes": n, "pass_rate": round(n / runs, 3), "passed": n == runs,
              "median_latency_ms": 5000, "worst_latency_ms": 9000} for qid, n in passes.items()]
    total, got = runs * len(per_q), sum(passes.values())
    data = {"phase": 1, "today": "2026-06-30", "model": "qwen3:8b", "agent": "real", "key": key, "mode": "auto",
            "repeat": repeat, "planner_enabled": False,
            "score": {"questions_passed": sum(1 for q in per_q if q["passed"]), "questions_total": len(per_q),
                      "runs_passed": got, "runs_total": total, "overall_pass_rate": round(got / total, 3)},
            "latency_ms": {"median": 5000, "worst": 9000}, "per_question": per_q, "failures": [], "results": []}
    data.update(extra)
    return data


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def run(tmp_path, old, new, *args):
    r = CliRunner().invoke(cmp.cli, [str(write(tmp_path, "old.json", old)), str(write(tmp_path, "new.json", new)), *args])
    return r


# ---------------------------------------------------------------- loading
def test_load_result_rejects_bad_files(tmp_path):
    with pytest.raises(cmp.CompareError, match="cannot read"):
        cmp.load_result(tmp_path / "nope.json")
    with pytest.raises(cmp.CompareError, match="not a result file"):
        cmp.load_result(write(tmp_path, "x.json", [1, 2]))
    with pytest.raises(cmp.CompareError, match="stub"):
        cmp.load_result(write(tmp_path, "s.json", rf({"Q1": 3}, agent="stub")))
    bad = rf({"Q1": 3})
    bad["per_question"][0].pop("passes")
    with pytest.raises(cmp.CompareError, match="lacks"):
        cmp.load_result(write(tmp_path, "b.json", bad))


# ---------------------------------------------------------------- the floor
def test_the_floor_values_are_the_recorded_phase1_ranges():
    assert config.REGRESSION_FLOORS == {"answer_key.json": (22, 30), "heldout_key.json": (15, 30)}
    assert cmp.floor_required("answer_key.json", 30) == 22
    assert cmp.floor_required("heldout_key.json", 30) == 15


@pytest.mark.parametrize("key,total,expected", [("answer_key.json", 10, 8), ("answer_key.json", 20, 15),
                                                ("heldout_key.json", 10, 5), ("heldout_key.json", 60, 30),
                                                ("answer_key.json", 0, 1)])
def test_the_floor_scales_to_other_run_counts_rounding_up(key, total, expected):
    assert cmp.floor_required(key, total) == expected


def test_no_floor_for_other_keys():
    assert cmp.floor_required("phase2_key.json", 30) is None
    assert cmp.floor_required("phase2_heldout_key.json", 30) is None


def split(n, qs=10, runs=3):
    """n passes spread over qs questions of `runs` runs each."""
    out = {f"Q{i + 1}": 0 for i in range(qs)}
    for i in range(qs):
        take = min(runs, n)
        out[f"Q{i + 1}"], n = take, n - take
    return out


@pytest.mark.parametrize("passed,exit_code", [(30, 0), (23, 0), (22, 0), (21, 1), (10, 1)])
def test_original_key_floor_is_22_of_30(tmp_path, passed, exit_code):
    r = run(tmp_path, rf(split(23)), rf(split(passed)))
    assert r.exit_code == exit_code, r.output
    assert ("REGRESSION" in r.output) == bool(exit_code)


@pytest.mark.parametrize("passed,exit_code", [(18, 0), (15, 0), (14, 1), (0, 1)])
def test_heldout_key_floor_is_15_of_30(tmp_path, passed, exit_code):
    r = run(tmp_path, rf(split(18), key="heldout_key.json"), rf(split(passed), key="heldout_key.json"))
    assert r.exit_code == exit_code, r.output


def test_the_gate_is_on_new_not_old(tmp_path):
    r = run(tmp_path, rf(split(10)), rf(split(25)))           # OLD below the floor, NEW fine
    assert r.exit_code == 0 and "-> ok" in r.output


def test_phase2_keys_have_no_floor_and_no_floor_flag_disables_it(tmp_path):
    r = run(tmp_path, rf(split(20), key="phase2_key.json"), rf(split(0), key="phase2_key.json"))
    assert r.exit_code == 0 and "none applies" in r.output
    r = run(tmp_path, rf(split(23)), rf(split(5)), "--no-floor")
    assert r.exit_code == 0 and "REGRESSION" not in r.output


# ---------------------------------------------------------------- per-question deltas
def test_verdicts_per_question():
    old = rf({"Q1": 3, "Q2": 3, "Q3": 1, "Q4": 1, "Q5": 0, "Q6": 2})
    new = rf({"Q1": 1, "Q2": 3, "Q3": 0, "Q4": 2, "Q5": 3, "Q6": 2})
    rows = {r["id"]: r for r in cmp.compare(old, new)["rows"]}
    assert rows["Q1"]["verdict"] == "REGRESSED" and rows["Q1"]["delta_points"] == -67
    assert rows["Q2"]["verdict"] == "same" and rows["Q2"]["delta_points"] == 0
    assert rows["Q3"]["verdict"] == "worse"
    assert rows["Q4"]["verdict"] == "better"
    assert rows["Q5"]["verdict"] == "FIXED" and rows["Q5"]["delta_points"] == 100
    assert rows["Q6"]["verdict"] == "same"


def test_failure_reasons_are_shown_only_for_questions_that_got_worse():
    new = rf({"Q1": 1, "Q2": 0}, failures=[{"id": "Q1", "run": 1, "reasons": ["forbidden citation ['ATL-1']"]},
                                          {"id": "Q1", "run": 2, "reasons": ["forbidden citation ['ATL-1']"]},
                                          {"id": "Q2", "run": 1, "reasons": ["status=loop_cap"]}])
    rows = {r["id"]: r for r in cmp.compare(rf({"Q1": 3, "Q2": 0}), new)["rows"]}
    assert rows["Q1"]["new_reasons"] == ["forbidden citation ['ATL-1']"]        # de-duplicated
    assert rows["Q2"]["new_reasons"] == []                                       # already failing before: not new


def test_questions_missing_from_one_file_are_listed_not_dropped():
    rep = cmp.compare(rf({"Q1": 3, "Q2": 3}), rf({"Q1": 3, "Q3": 3}))
    assert [r["id"] for r in rep["rows"]] == ["Q1"] and rep["added"] == ["Q3"] and rep["removed"] == ["Q2"]


def test_comparing_different_keys_is_an_error(tmp_path):
    with pytest.raises(cmp.CompareError, match="different answer keys"):
        cmp.compare(rf({"Q1": 3}), rf({"Q1": 3}, key="heldout_key.json"))
    r = run(tmp_path, rf({"Q1": 3}), rf({"Q1": 3}, key="heldout_key.json"))
    assert r.exit_code == 2 and "different answer keys" in r.output


def test_cli_exit_2_for_unreadable_or_stub_files(tmp_path):
    good = write(tmp_path, "g.json", rf({"Q1": 3}))
    assert CliRunner().invoke(cmp.cli, [str(good), str(tmp_path / "missing.json")]).exit_code == 2
    stub = write(tmp_path, "s.json", rf({"Q1": 3}, agent="stub"))
    assert CliRunner().invoke(cmp.cli, [str(good), str(stub)]).exit_code == 2


# ---------------------------------------------------------------- Phase 2 details
def test_planner_steps_tokens_are_compared_when_both_files_report_them():
    totals = lambda tok, steps, planner=None: {"tokens": {"median_per_run": tok}, "steps": {"median_per_run": steps},
                                               "planner": planner or {}}
    old = rf({"Q1": 2}, key="phase2_key.json", totals=totals(1000, 5), planner_enabled=False)
    new = rf({"Q1": 3}, key="phase2_key.json", planner_enabled=True,
             totals=totals(2400, 9, {"runs": 3, "invalid": 1, "fallback": 1}))
    text = cmp.render(cmp.compare(old, new), "flat.json", "planner.json")
    assert "OLD: flat.json (planner off)" in text and "NEW: planner.json (planner on)" in text
    assert "STEPS:     median per run 5 -> 9" in text and "TOKENS:    median per run 1000 -> 2400" in text
    assert "PLANNER:   3 planned runs in NEW, 1 invalid plan(s), 1 fallback(s)" in text
    assert "FIXED" in text                                   # 2/3 -> 3/3


# ---------------------------------------------------------------- comparability warnings
def warnings_for(old_extra, new_extra, **new_top):
    return cmp.compare(rf({"Q1": 3}, **old_extra), rf({"Q1": 3}, **new_extra, **new_top))["warnings"]


def test_matching_environments_give_no_warning():
    assert warnings_for({"environment": ENV}, {"environment": ENV}) == []


def test_files_without_an_environment_record_are_called_out():
    w = warnings_for({}, {"environment": ENV})
    assert len(w) == 1 and w[0].startswith("OLD has no environment record")
    assert warnings_for({"environment": ENV}, {})[0].startswith("NEW has no environment record")
    assert warnings_for({}, {})[0].startswith("Neither file has environment record")


def test_different_machine_model_digest_repeat_and_faults_are_flagged():
    other = {**ENV, "machine": {"node": "box-2"}}
    assert any("different machines" in w for w in warnings_for({"environment": ENV}, {"environment": other}))
    digest = {**ENV, "model_fingerprint": {"name": "m", "digest": "zzz"}}
    assert any("digest differs" in w for w in warnings_for({"environment": ENV}, {"environment": digest}))
    nodigest = {**ENV, "model_fingerprint": None}
    assert any("digest not recorded" in w for w in warnings_for({"environment": ENV}, {"environment": nodigest}))
    faulty = {**ENV, "faults": {"notion": "fail"}}
    assert any("NEW was recorded with fault injection" in w for w in warnings_for({"environment": ENV}, {"environment": faulty}))
    w = cmp.compare(rf({"Q1": 3}, repeat=3), rf({"Q1": 1}, repeat=1, runs=1))["warnings"]
    assert any("repeat differs" in x for x in w)
    assert any("model differs" in x for x in warnings_for({}, {}, model="other:1b"))
    assert any("grading mode differs" in x for x in warnings_for({}, {}, mode="manual"))
    assert any("TODAY differs" in x for x in warnings_for({}, {}, today="2026-07-01"))


# ---------------------------------------------------------------- the recorded Phase 1 files
@pytest.mark.parametrize("name", ["phase1_baseline.json", "phase1_heldout.json"])
def test_the_recorded_phase1_results_meet_their_own_floor(name):
    """If this fails, either a recorded file was overwritten with a worse run or the floor was set above it."""
    data = cmp.load_result(ROOT / "evals" / "results" / name)
    rep = cmp.compare(data, data)
    assert rep["floor"] and rep["floor"]["ok"] and not rep["regression"]
    assert all(r["verdict"] == "same" for r in rep["rows"])
