"""Shared test setup. OWNER: C.

Result files describe the machine and the Ollama model they were recorded on (Day 0 decision 8). The model
probe talks to a local Ollama server, so tests never run it: no test may depend on, or wait for, a network call.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_model_probe(monkeypatch):
    from evals import run_evals

    monkeypatch.setattr(run_evals, "model_fingerprint", lambda model: None)


@pytest.fixture(autouse=True)
def _tests_do_not_depend_on_the_shell(monkeypatch):
    """A switch left in the person's shell (PLANNER_ENABLED, ENABLED_SOURCES, FAULT_INJECT) must not change test
    outcomes, and a test that widens the source registry must not leak into the next one."""
    from app import config, sources

    for var in ("PLANNER_ENABLED", "ENABLED_SOURCES", "FAULT_INJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "PLANNER_ENABLED", False)  # config reads the variable once, at import
    monkeypatch.setattr(sources, "_REGISTRY", dict(sources._REGISTRY))
