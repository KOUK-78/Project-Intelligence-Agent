import json

from typer.testing import CliRunner

from app import config, tracing
from app.cli import app
from evals.run_evals import failure_reasons, mechanical_checks, summarize


def test_config_contract():
    assert config.TODAY == "2026-06-30"
    assert config.MAX_AGENT_STEPS == 5 and config.NUM_CTX == 8192
    assert set(config.MODELS) >= {"agent", "synthesizer"}


def test_cli_stub_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACING_ENABLED", False)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")
    r = CliRunner().invoke(app, ["ask", "--stub", "What is blocking Project Atlas?"])
    assert r.exit_code == 0 and "ATL-212" in r.output and "Status:" in r.output
    line = json.loads((tmp_path / "runs.jsonl").read_text().splitlines()[0])
    assert line["citations"] == ["ATL-212"] and line["status"] == "ok"


def test_observe_run_turns_exceptions_into_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")
    res = tracing.observe_run("q", lambda q: 1 / 0)
    assert res["status"] == "error"


def test_mechanical_checks_and_summary():
    res = {"citations": ["ATL-212", "ATL-999"]}
    chk = mechanical_checks(["ATL-212"], res, {"ATL-212"})
    assert chk["all_expected_citations_found"] and chk["invalid_citations"] == ["ATL-999"]
    rec = {"id": "Q1", "status": "ok", "latency_ms": 5, "correct": True, **chk}
    assert failure_reasons(rec) == ["invalid citation ['ATL-999']"]
    assert "SCORE: 1 of 1 correct" in summarize([rec])
