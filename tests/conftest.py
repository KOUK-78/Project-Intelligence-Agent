"""Shared test setup. OWNER: C.

Result files describe the machine and the Ollama model they were recorded on (Day 0 decision 8). The model
probe talks to a local Ollama server, so tests never run it: no test may depend on, or wait for, a network call.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_model_probe(monkeypatch):
    from evals import run_evals

    monkeypatch.setattr(run_evals, "model_fingerprint", lambda model: None)
