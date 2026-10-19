"""Single source of truth for settings. Model names appear ONLY here."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

# --- Models (one entry per job; all qwen3:8b for now; one loaded at a time on 8 GB VRAM)
MODELS = {
    "agent": "qwen3:8b",
    "synthesizer": "qwen3:8b",
    "judge": "qwen3:8b",          # used from Phase 4
    "embedding": "nomic-embed-text",
}
# Thinking off for agent/synthesizer (pass as reasoning=False to ChatOllama)
THINKING = {"agent": False, "synthesizer": False}

# --- Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
NUM_CTX = 8192            # keep at 8192 on 8 GB cards
TEMPERATURE = 0.1

# --- Reproducibility: never use the real system date
TODAY = "2026-06-30"

# --- Agent
MAX_AGENT_STEPS = 5

# --- Paths
DATA_PATH = ROOT / "data" / "synthetic" / "tickets.json"
LOGS_DIR = ROOT / "logs"
RUNS_LOG = LOGS_DIR / "runs.jsonl"
ANSWER_KEY_PATH = ROOT / "evals" / "answer_key.json"
RESULTS_DIR = ROOT / "evals" / "results"

# --- Tracing (Arize Phoenix, local)
TRACING_ENABLED = (os.getenv("TRACING_ENABLED") or "1").lower() not in ("0", "false", "no")
PHOENIX_PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME") or "project-intelligence-agent"
PHOENIX_COLLECTOR_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or "http://localhost:6006/v1/traces"
