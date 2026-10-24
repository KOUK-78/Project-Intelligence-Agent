import json
import os

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


def test_cli_heldout_flag_uses_heldout_key_and_file(monkeypatch, tmp_path):
    hk = tmp_path / "heldout_key.json"
    hk.write_text(json.dumps([q("H1")]))
    monkeypatch.setattr(config, "HELDOUT_KEY_PATH", hk)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: (lambda _q: result(["ATL-112"])))
    monkeypatch.setattr(ev, "load_valid_ids", lambda *a, **k: VALID)
    r = CliRunner().invoke(ev.cli, ["--heldout", "--auto"])
    assert r.exit_code == 0, r.output
    data = json.loads((tmp_path / "phase1_heldout.json").read_text())
    assert data["key"] == "heldout_key.json" and data["results"][0]["id"] == "H1"


def test_cli_heldout_and_key_conflict(monkeypatch, tmp_path):
    k = tmp_path / "k.json"
    k.write_text(json.dumps([q()]))
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: (lambda _q: result(["ATL-112"])))
    r = CliRunner().invoke(ev.cli, ["--heldout", "--key", str(k)])
    assert r.exit_code != 0


# ================================================================ Phase 2 (C2)
VALID2 = VALID | {"NTN-201", "EML-301"}


def p2q(qid="P1", expected=("ATL-112", "NTN-201"), sources=("jira", "notion"), mention=("sept 15",), forbidden=None):
    d = q(qid, expected, forbidden)
    d["sources_required"] = list(sources)
    d["must_mention"] = list(mention)
    return d


def p2result(cites=("ATL-112", "NTN-201"), answer="It ships Sept 15.", **extra):
    r = result(list(cites))
    r["answer"] = answer
    r.update(extra)
    return r


# ---------------------------------------------------------------- sources and wording checks
def test_source_of_uses_the_prefix_registry():
    assert [ev.source_of(c) for c in ("ATL-101", "NTN-201", "EML-301", "XYZ-1", "nonsense")] == \
           ["jira", "notion", "email", None, None]
    assert ev.sources_of(["EML-301", "ATL-101", "ATL-102", "XYZ-9"]) == ["jira", "email"]  # config order, distinct


def test_required_sources_and_must_mention_are_checked():
    chk = ev.mechanical_checks(["ATL-112"], p2result(["ATL-112"], "Ships SEPT   15."), VALID2, [], ["jira", "notion"], ["sept 15"])
    assert chk["sources_cited"] == ["jira"] and chk["sources_missing"] == ["notion"] and not chk["all_sources_covered"]
    assert chk["all_must_mention_found"]          # case and whitespace do not matter
    chk = ev.mechanical_checks(["ATL-112"], p2result(answer="no date given"), VALID2, [], [], ["sept 15", "no date"])
    assert chk["must_mention_missing"] == ["sept 15"] and not chk["all_must_mention_found"]


def test_auto_pass_needs_sources_and_text_but_phase1_records_are_unaffected():
    ok = ev.mechanical_checks(["ATL-112", "NTN-201"], p2result(), VALID2, [], ["jira", "notion"], ["sept 15"])
    assert ev.auto_pass({"status": "ok", **ok})
    no_src = ev.mechanical_checks(["ATL-112"], p2result(["ATL-112"]), VALID2, [], ["jira", "notion"], [])
    assert not ev.auto_pass({"status": "ok", **no_src})
    no_text = ev.mechanical_checks(["ATL-112"], p2result(["ATL-112"], "x"), VALID2, [], [], ["sept 15"])
    assert not ev.auto_pass({"status": "ok", **no_text})
    old = {k: v for k, v in ev.mechanical_checks(["ATL-112"], result(["ATL-112"]), VALID).items()
           if k in ("all_expected_citations_found", "any_forbidden_citation", "any_invalid_citation")}
    assert ev.auto_pass({"status": "ok", **old})  # a Phase 1 record has none of the new fields


def test_failure_reasons_name_the_new_checks():
    chk = ev.mechanical_checks(["ATL-112"], p2result(["ATL-112"], "x"), VALID2, [], ["notion"], ["sept 15"])
    why = " | ".join(ev.failure_reasons({"status": "ok", **chk}))
    assert "required source not cited ['notion']" in why and "answer missing required text ['sept 15']" in why


def test_load_key_validates_the_phase2_fields(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps([p2q()]))
    assert ev.load_key(p)[0]["sources_required"] == ["jira", "notion"]
    for field, value, message in [("sources_required", "jira", "must be a list"),
                                  ("sources_required", ["jira", "slack"], "unknown source"),
                                  ("must_mention", "sept 15", "must be a list"),
                                  ("must_mention", ["ok", "  "], "non-empty strings")]:
        bad = p2q()
        bad[field] = value
        p.write_text(json.dumps([bad]))
        with pytest.raises(typer.BadParameter, match=message):
            ev.load_key(p)


# ---------------------------------------------------------------- result file names
def test_phase2_result_files_depend_on_key_and_planner(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    name = lambda key, planner, stub=False: ev.default_out_path(key, stub, planner).name
    assert name("evals/phase2_key.json", False) == "phase2_flat.json"
    assert name("evals/phase2_key.json", True) == "phase2_planner.json"
    assert name("evals/phase2_heldout_key.json", True) == "phase2_heldout.json"
    assert name("evals/phase2_heldout_key.json", False) == "phase2_heldout_flat.json"
    assert name("evals/phase2_key.json", True, stub=True) == "phase2_planner.stub.json"


def test_a_planner_run_never_overwrites_a_phase1_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    assert ev.default_out_path("evals/answer_key.json", False, False).name == "phase1_baseline.json"
    assert ev.default_out_path("evals/answer_key.json", False, True).name == "phase1_baseline_planner.json"
    assert ev.default_out_path("evals/heldout_key.json", False, True).name == "phase1_heldout_planner.json"
    monkeypatch.setattr(config, "PLANNER_ENABLED", True)   # default follows the config switch
    assert ev.default_out_path("evals/answer_key.json", False).name == "phase1_baseline_planner.json"


# ---------------------------------------------------------------- optional AgentResult fields
def sq(sid="sq1", source="jira", status="resolved", steps=2, reason="r", unavailable=()):
    """One sub-question record in the shape the agent emits (CONTRACTS.md section 4)."""
    return {"id": sid, "question": "q", "source": source, "reason": reason, "status": status, "outcome": "model",
            "steps": steps, "attempts": 1, "rewritten": None, "capped": False, "unavailable": list(unavailable)}


def test_phase2_fields_tolerate_absence():
    f = ev.phase2_fields(result(["ATL-112"]))
    assert f["planner_used"] is False and f["planner_valid"] is None and f["subquestion_count"] is None
    assert f["steps_total"] is None and f["total_tokens"] is None and f["sources_returning_records"] is None
    assert f["tool_call_count"] == 0 and f["sources_unavailable"] is None
    junk = ev.phase2_fields({"plan": "oops", "token_usage": [1], "sources": 3, "tool_calls": None})
    assert junk["planner_used"] is False and junk["total_tokens"] is None and junk["sources_returning_records"] is None
    assert ev.phase2_fields({"plan": {"valid": True}})["planner_used"] is False       # the old dict shape is not the contract


def test_phase2_fields_read_the_agents_plan_list():
    plan = [sq("sq1", "jira", "resolved", 2), sq("sq2", "notion", "unresolved", 5), sq("sq3", "email", "unavailable", 1, unavailable=["email"])]
    f = ev.phase2_fields(result(["ATL-112"]) | {"plan": plan, "sources": ["jira"],
                                                "token_usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100, "calls": 3}})
    assert (f["planner_used"], f["planner_valid"], f["planner_fallback"]) == (True, True, False)
    assert (f["subquestion_count"], f["subquestions_unresolved"], f["steps_total"], f["total_tokens"]) == (3, 1, 8, 100)
    assert f["sources_returning_records"] == ["jira"] and f["sources_unavailable"] == ["email"]
    plan[1]["steps"] = None                              # an unreported step count means the total is unknown
    assert ev.phase2_fields({"plan": plan})["steps_total"] is None


def test_a_planner_fallback_is_an_invalid_plan():
    fb = [sq("sq1", "all", "resolved", 3, reason="planner fallback: one question over all sources")]
    f = ev.phase2_fields({"plan": fb})
    assert (f["planner_used"], f["planner_valid"], f["planner_fallback"], f["subquestion_count"]) == (True, False, True, 1)


def test_totals_count_only_runs_that_reported():
    ok_plan = [sq("sq1", steps=3), sq("sq2", steps=1)]
    bad_plan = [sq("sq1", "all", steps=4, reason="planner fallback: one question over all sources")]
    replies = iter([p2result(plan=ok_plan, token_usage={"total_tokens": 100}),
                    p2result(["ATL-112"], plan=bad_plan, token_usage={"total_tokens": 300}),
                    p2result()])                                   # reports nothing
    recs = ev.run_questions([p2q()], lambda _q: next(replies), 3, VALID2, lambda qq, r: ev.auto_pass(r))
    t = ev.aggregate(recs)
    assert t["tokens"] == {"runs_reporting": 2, "total": 400, "median_per_run": 200}
    assert t["steps"]["total"] == 8 and t["steps"]["runs_reporting"] == 2
    assert t["planner"] == {"runs": 2, "invalid": 1, "fallback": 1, "median_subquestions": 1.5}
    assert t["source_checks"] == {"runs_with_required_sources": 3, "runs_covering_them": 2,
                                  "runs_with_must_mention": 3, "runs_mentioning_all": 3}
    text = ev.summarize(recs)
    assert "PLANNER: 2 runs planned, 1 invalid plan(s), 1 fallback(s)" in text
    assert "TOKENS: 400 total" in text and "SOURCES: 2 of 3 runs" in text


def test_phase1_summary_has_no_phase2_lines():
    recs = ev.run_questions([q()], lambda _q: result(["ATL-112"]), 1, VALID, lambda qq, r: ev.auto_pass(r))
    text = ev.summarize(recs)
    assert not any(word in text for word in ("PLANNER", "TOKENS", "STEPS:", "SOURCES", "MUST MENTION"))


# ---------------------------------------------------------------- multi-source ID loading
def test_collect_ids_walks_any_layout_and_ignores_other_ids():
    doc = {"pages": [{"id": "NTN-201", "sections": [{"id": "NTN-201", "body": "x"}],
                      "author": {"id": "USR-7"}}], "emails": [{"id": "EML-301", "thread": {"id": "THR-1"}}]}
    assert ev.collect_ids(doc, {"NTN"}) == {"NTN-201"}
    assert ev.collect_ids(doc, {"NTN", "EML"}) == {"NTN-201", "EML-301"}
    assert ev.collect_ids({"id": "NTN-x1"}, {"NTN"}) == set()          # malformed ID


def test_load_extra_ids_reports_missing_sources(monkeypatch, tmp_path):
    notion = tmp_path / "n.json"
    notion.write_text(json.dumps({"pages": [{"id": "NTN-201"}]}))
    monkeypatch.setattr(config, "DATA_PATHS", {"jira": tmp_path / "j.json", "notion": notion, "email": tmp_path / "gone.json"})
    ids, missing = ev.load_extra_ids()
    assert ids == {"NTN-201"} and missing == ["email"]


# ---------------------------------------------------------------- CLI (Phase 2)
def run_p2(monkeypatch, tmp_path, agent, *args, questions=None, planner_default=False, valid=None, extra=None):
    key = tmp_path / ("phase2_heldout_key.json" if "--heldout" in args else "phase2_key.json")
    key.write_text(json.dumps(questions or [p2q()]))
    monkeypatch.setattr(config, "PHASE2_KEY_PATH", tmp_path / "phase2_key.json")
    monkeypatch.setattr(config, "PHASE2_HELDOUT_KEY_PATH", tmp_path / "phase2_heldout_key.json")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(config, "PLANNER_ENABLED", planner_default)
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: agent)
    monkeypatch.setattr(ev, "load_valid_ids", lambda *a, **k: VALID2 if valid is None else valid)
    if extra is not None:
        monkeypatch.setattr(ev, "load_extra_ids", lambda: extra)
    monkeypatch.delenv("FAULT_INJECT", raising=False)
    return CliRunner().invoke(ev.cli, ["--phase2", *args])


def test_cli_phase2_planner_off_writes_the_flat_file(monkeypatch, tmp_path):
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--no-planner", "--auto", "--repeat", "2")
    assert r.exit_code == 0, r.output
    data = json.loads((tmp_path / "phase2_flat.json").read_text())
    assert data["phase"] == 2 and data["planner_enabled"] is False and data["key"] == "phase2_key.json"
    assert data["score"]["runs_passed"] == 2 and "source_checks" in data["totals"]
    assert {"machine", "models", "git_commit", "settings", "faults"} <= set(data["environment"])
    assert data["environment"]["machine"]["node"] and data["environment"]["faults"] == {}
    assert not (tmp_path / "phase2_planner.json").exists()


def test_cli_phase2_planner_on_writes_the_planner_file_and_the_agent_sees_the_switch(monkeypatch, tmp_path):
    seen = []

    def agent(_q):
        seen.append((config.PLANNER_ENABLED, os.environ.get("PLANNER_ENABLED")))
        return p2result()

    r = run_p2(monkeypatch, tmp_path, agent, "--planner", "--auto")
    assert r.exit_code == 0, r.output
    assert seen == [(True, "1")]
    assert json.loads((tmp_path / "phase2_planner.json").read_text())["planner_enabled"] is True


def test_cli_planner_switch_is_restored_after_the_run(monkeypatch, tmp_path):
    monkeypatch.setenv("PLANNER_ENABLED", "0")
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--planner", "--auto")
    assert r.exit_code == 0, r.output
    assert config.PLANNER_ENABLED is False and os.environ["PLANNER_ENABLED"] == "0"
    monkeypatch.delenv("PLANNER_ENABLED")
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--planner", "--auto")
    assert "PLANNER_ENABLED" not in os.environ


def test_cli_planner_defaults_to_the_config_switch(monkeypatch, tmp_path):
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--auto", planner_default=True)
    assert r.exit_code == 0, r.output
    assert (tmp_path / "phase2_planner.json").exists()


def test_cli_phase2_heldout_files_and_banner(monkeypatch, tmp_path):
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--heldout", "--planner", "--auto")
    assert r.exit_code == 0, r.output
    assert "MEASUREMENT ONLY" in r.output
    assert json.loads((tmp_path / "phase2_heldout.json").read_text())["key"] == "phase2_heldout_key.json"
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--heldout", "--no-planner", "--auto")
    assert (tmp_path / "phase2_heldout_flat.json").exists()


def test_cli_phase2_fails_when_a_required_source_is_not_cited(monkeypatch, tmp_path):
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(["ATL-112"]), "--no-planner", "--auto")
    assert r.exit_code == 1
    assert "required source not cited ['notion']" in r.output


def test_cli_phase2_and_key_conflict(monkeypatch, tmp_path):
    k = tmp_path / "k.json"
    k.write_text(json.dumps([q()]))
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: (lambda _q: result(["ATL-112"])))
    r = CliRunner().invoke(ev.cli, ["--phase2", "--key", str(k)])
    assert r.exit_code != 0


def test_cli_warns_when_phase2_data_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_PATHS", {"jira": config.DATA_PATH, "notion": tmp_path / "no.json", "email": tmp_path / "no2.json"})
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--no-planner", "--auto")
    assert "no data file for ['notion', 'email']" in r.output


def test_cli_flags_a_fault_injected_run_and_records_it(monkeypatch, tmp_path):
    key = tmp_path / "k.json"
    key.write_text(json.dumps([q()]))
    monkeypatch.setattr(ev, "get_run_agent", lambda use_stub=False: (lambda _q: result(["ATL-112"])))
    monkeypatch.setattr(ev, "load_valid_ids", lambda *a, **k: VALID)
    monkeypatch.setenv("FAULT_INJECT", "notion:fail")
    out = tmp_path / "o.json"
    r = CliRunner().invoke(ev.cli, ["--key", str(key), "--out", str(out), "--auto"])
    assert "FAULT_INJECT is active" in r.output and "not a measurement" in r.output
    assert json.loads(out.read_text())["environment"]["faults"] == {"notion": "fail"}


def test_extra_source_ids_are_accepted_and_the_callers_set_is_never_modified(monkeypatch, tmp_path):
    shared = {"ATL-112"}                                   # Jira only, as load_valid_ids returns it
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--no-planner", "--auto",
               valid=shared, extra=({"NTN-201", "EML-301"}, []))
    assert r.exit_code == 0, r.output                       # NTN-201 is valid through the extra IDs
    assert shared == {"ATL-112"}                            # a new set was built, not the caller's mutated
    r = run_p2(monkeypatch, tmp_path, lambda _q: p2result(), "--no-planner", "--auto",
               valid=shared, extra=(set(), []))
    assert r.exit_code == 1 and "invalid citation ['NTN-201']" in r.output


def test_phase1_run_records_phase_1_and_planner_off(monkeypatch, tmp_path):
    r, out = run_cli(monkeypatch, tmp_path, [q()], lambda _q: result(["ATL-112"]), "--auto")
    data = json.loads(out.read_text())
    assert r.exit_code == 0 and data["phase"] == 1 and data["planner_enabled"] is False


def test_must_mention_matches_whole_tokens_only():
    assert ev.mentions("it was 25 days late", "25") and ev.mentions("late by 25.", "25") and ev.mentions("25% done", "25")
    assert not ev.mentions("due in 2025", "25") and not ev.mentions("250 tickets", "25") and not ev.mentions("v25a", "25")
    assert ev.mentions("ga is 2026-09-15 for all", "2026-09-15") and ev.mentions("sept 15", "SEPT   15")
    assert not ev.mentions("anything", "") and not ev.mentions("", "x")
