import json

import pytest
import typer
from typer.testing import CliRunner

from app import config
from evals import run_evals as ev

VALID = {"ATL-101", "ATL-109", "ATL-112"}


def result(cites, status="ok", latency=100, trace="t1"):
    return {"answer": "a", "citations": cites, "status": status, "latency_ms": latency,
            "trace_id": trace, "tool_calls": []}


def q(qid="Q1", expected=("ATL-112",), forbidden=None):
    d = {"id": qid, "question": f"question {qid}?", "expected_answer": "x",
         "expected_citations": list(expected), "notes": ""}
    if forbidden is not None:
        d["forbidden_citations"] = forbidden
    return d


# ---------------------------------------------------------------- checks
def test_mechanical_checks_forbidden_and_invalid():
    chk = ev.mechanical_checks(["ATL-112"], result(["ATL-112", "ATL-109", "ATL-999"]), VALID, ["ATL-109"])
    assert chk["all_expected_citations_found"]
    assert chk["forbidden_citations_hit"] == ["ATL-109"] and chk["any_forbidden_citation"]
    assert chk["invalid_citations"] == ["ATL-999"]


def test_forbidden_field_is_optional():
    chk = ev.mechanical_checks(["ATL-112"], result(["ATL-112", "ATL-109"]), VALID)  # no forbidden given
    assert not chk["any_forbidden_citation"]


@pytest.mark.parametrize("cites,status,ok", [
    (["ATL-112"], "ok", True),
    (["ATL-112", "ATL-109"], "ok", False),   # forbidden
    ([], "ok", False),                        # missing expected
    (["ATL-112", "ATL-999"], "ok", False),   # invalid id
    (["ATL-112"], "loop_cap", False),         # bad status
])
def test_auto_pass(cites, status, ok):
    chk = ev.mechanical_checks(["ATL-112"], result(cites), VALID, ["ATL-109"])
    assert ev.auto_pass({"status": status, **chk}) is ok


def test_failure_reasons_mentions_forbidden():
    chk = ev.mechanical_checks(["ATL-112"], result(["ATL-112", "ATL-109"]), VALID, ["ATL-109"])
    why = ev.failure_reasons({"status": "ok", "correct": False, **chk})
    assert any("forbidden" in w for w in why)


# ---------------------------------------------------------------- key loading / paths
def test_load_key_validates(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps([q("Q1"), q("Q1")]))
    with pytest.raises(typer.BadParameter, match="duplicate"):
        ev.load_key(p)
    p.write_text(json.dumps([{"id": "Q1", "question": "x"}]))
    with pytest.raises(typer.BadParameter, match="missing"):
        ev.load_key(p)
    p.write_text(json.dumps([q("Q1", forbidden="ATL-109")]))
    with pytest.raises(typer.BadParameter, match="must be a list"):
        ev.load_key(p)
    p.write_text(json.dumps([q("Q1", forbidden=["ATL-109"])]))
    assert ev.load_key(p)[0]["forbidden_citations"] == ["ATL-109"]


def test_default_out_paths_are_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    assert ev.default_out_path("evals/answer_key.json", False).name == "phase1_baseline.json"
    assert ev.default_out_path("evals/heldout_key.json", False).name == "phase1_heldout.json"
    assert ev.default_out_path("evals/answer_key.json", True).name == "phase1_baseline.stub.json"


# ---------------------------------------------------------------- repeat / aggregate
def test_repeat_pass_rate_and_latency():
    replies = iter([result(["ATL-112"], latency=100), result([], latency=300), result(["ATL-112"], latency=200)])
    recs = ev.run_questions([q()], lambda _q: next(replies), 3, VALID, lambda qq, r: ev.auto_pass(r))
    agg = ev.aggregate(recs)
    only = agg["questions"][0]
    assert (only["runs"], only["passes"], only["pass_rate"]) == (3, 2, 0.667)
    assert only["median_latency_ms"] == 200 and only["worst_latency_ms"] == 300
    assert agg["questions_passed"] == 0                       # needs 100% by default
    assert ev.aggregate(recs, min_pass_rate=0.6)["questions_passed"] == 1
    assert agg["overall_pass_rate"] == 0.667


def test_ungraded_is_not_a_pass():
    recs = ev.run_questions([q()], lambda _q: result(["ATL-112"]), 1, VALID, lambda qq, r: None)
    assert ev.aggregate(recs)["questions_passed"] == 0


def test_summary_text():
    recs = ev.run_questions([q()], lambda _q: result(["ATL-112"]), 2, VALID, lambda qq, r: ev.auto_pass(r))
    text = ev.summarize(recs)
    assert "SCORE: 1 of 1 questions pass" in text and "RUNS:  2 of 2 passed" in text


# ---------------------------------------------------------------- CLI
def run_cli(monkeypatch, tmp_path, questions, agent, *args):
    key = tmp_path / "key.json"
    key.write_text(json.dumps(questions))
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: agent)
    monkeypatch.setattr(ev, "load_valid_ids", lambda *a, **k: VALID)
    out = tmp_path / "out.json"
    r = CliRunner().invoke(ev.cli, ["--key", str(key), "--out", str(out), *args])
    return r, out


def test_cli_auto_pass_exit_zero(monkeypatch, tmp_path):
    r, out = run_cli(monkeypatch, tmp_path, [q()], lambda _q: result(["ATL-112"]), "--auto", "--repeat", "2")
    assert r.exit_code == 0, r.output
    data = json.loads(out.read_text())
    assert data["mode"] == "auto" and data["repeat"] == 2 and len(data["results"]) == 2
    assert data["score"]["runs_passed"] == 2 and data["failures"] == []


def test_cli_auto_forbidden_citation_exits_nonzero(monkeypatch, tmp_path):
    r, out = run_cli(monkeypatch, tmp_path, [q(forbidden=["ATL-109"])],
                     lambda _q: result(["ATL-112", "ATL-109"]), "--auto")
    assert r.exit_code == 1
    assert "forbidden citation" in r.output
    assert json.loads(out.read_text())["failures"][0]["reasons"]


def test_cli_auto_never_prompts(monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("auto mode must not prompt"))
    r, _ = run_cli(monkeypatch, tmp_path, [q()], lambda _q: result(["ATL-112"]), "--auto")
    assert r.exit_code == 0


def test_cli_auto_and_no_grade_conflict(monkeypatch, tmp_path):
    r, _ = run_cli(monkeypatch, tmp_path, [q()], lambda _q: result(["ATL-112"]), "--auto", "--no-grade")
    assert r.exit_code != 0


def test_cli_require_trace(monkeypatch, tmp_path):
    r, _ = run_cli(monkeypatch, tmp_path, [q()], lambda _q: result(["ATL-112"], trace=None),
                   "--auto", "--require-trace")
    assert r.exit_code == 1 and "no trace_id" in r.output


def test_cli_stub_result_goes_to_stub_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    key = tmp_path / "heldout_key.json"
    key.write_text(json.dumps([q(expected=["ATL-112"])]))
    monkeypatch.setattr(config, "TRACING_ENABLED", False)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(ev, "load_valid_ids", lambda *a, **k: VALID)
    r = CliRunner().invoke(ev.cli, ["--key", str(key), "--stub", "--auto"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "phase1_heldout.stub.json").exists()
    assert not (tmp_path / "phase1_heldout.json").exists()
    assert json.loads((tmp_path / "phase1_heldout.stub.json").read_text())["agent"] == "stub"
