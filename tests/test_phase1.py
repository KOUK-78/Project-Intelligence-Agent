import json
import sys

import pytest
from typer.testing import CliRunner

from app import config, tracing
from app.cli import app
from app.runtime import get_run_agent


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACING_ENABLED", False)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(config, "RUNS_LOG", tmp_path / "runs.jsonl")


def test_config_contract():
    assert config.TODAY == "2026-06-30"
    assert config.MAX_AGENT_STEPS == 5 and config.NUM_CTX == 8192
    assert set(config.MODELS) >= {"agent", "synthesizer"}


def test_cli_stub_is_explicit_and_logs(tmp_path):
    r = CliRunner().invoke(app, ["ask", "--stub", "What is blocking Project Atlas?"])
    assert r.exit_code == 0 and "ATL-112" in r.output and "Status:" in r.output
    line = json.loads((tmp_path / "runs.jsonl").read_text().splitlines()[0])
    assert line["citations"] == ["ATL-112"] and line["status"] == "ok"


def test_no_silent_stub_fallback(monkeypatch):
    """If the real agent cannot be imported we must fail loudly, never fall back to the stub."""
    monkeypatch.setitem(sys.modules, "app.agent", None)  # makes `from app.agent import ...` raise ImportError
    with pytest.raises(RuntimeError, match="Cannot import the real agent"):
        get_run_agent(use_stub=False)


def test_cli_fails_loudly_when_agent_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.agent", None)
    r = CliRunner().invoke(app, ["ask", "anything"])
    assert r.exit_code != 0
    assert isinstance(r.exception, RuntimeError)
    assert "ATL-112" not in r.output  # no fake answer was printed


def test_observe_run_turns_exceptions_into_error():
    res = tracing.observe_run("q", lambda q: 1 / 0)
    assert res["status"] == "error"
