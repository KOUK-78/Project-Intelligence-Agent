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
PROJECT_NAME = "Atlas"             # the project every ticket belongs to; never a useful search term
AGENT_DEBUG = (os.getenv("AGENT_DEBUG") or "0").lower() not in ("0", "false", "no")  # per-node decision log on stderr
MAX_CALLS_PER_STEP = 6             # tool calls the model may issue in one decision
MAX_REF_TICKETS = 4                # tickets the graph reads in its follow-the-references round
MAX_CANDIDATE_TICKETS = 6          # tickets the graph reads in its read-every-candidate round
GRAPH_RECURSION_LIMIT = 40         # LangGraph backstop only; MAX_AGENT_STEPS is the real cap

# --- Timeouts (seconds)
MCP_CALL_TIMEOUT_S = 15            # one tool call
MCP_STARTUP_TIMEOUT_S = 20         # server launch + initialize + list_tools
LLM_TIMEOUT_S = 120                # one Ollama request

# --- Size caps (characters); keep prompts short, num_ctx is 8192 tokens
MAX_TOOL_CHARS = 2500              # one tool result fed back to the agent
EVIDENCE_ITEM_CHARS = 2000         # one tool result shown to the synthesizer
EVIDENCE_TOTAL_CHARS = 9000        # all evidence shown to the synthesizer
ERROR_TEXT_CHARS = 300             # error text kept in results and logs
SNIPPET_CHARS = 120                # short excerpt of a bad model/tool output

# --- Paths
DATA_PATH = ROOT / "data" / "synthetic" / "jira_tickets.json"
LOGS_DIR = ROOT / "logs"
RUNS_LOG = LOGS_DIR / "runs.jsonl"
ANSWER_KEY_PATH = ROOT / "evals" / "answer_key.json"
HELDOUT_KEY_PATH = ROOT / "evals" / "heldout_key.json"  # measurement only, never tuned against (CONTRACTS.md 7)
RESULTS_DIR = ROOT / "evals" / "results"

# --- Tracing (Arize Phoenix, local)
TRACING_ENABLED = (os.getenv("TRACING_ENABLED") or "1").lower() not in ("0", "false", "no")
PHOENIX_PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME") or "project-intelligence-agent"
PHOENIX_COLLECTOR_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or "http://localhost:6006/v1/traces"

# --- Heuristics (B2 audit). Graph-side helps for a small model. Each has a one-line note saying why it
# is general or question-specific. Specific ones default OFF until the held-out score shows they help.
# Override without editing code: AGENT_HEURISTICS_ON=ref_round,candidate_round  AGENT_HEURISTICS_OFF=filter_guard
HEURISTIC_DEFAULTS = {
    "detail_nudge": True,       # general: any why/when/change question lives in comments, so open the tickets
    "filter_guard": True,       # general: drops status/label filters the question never asked for (invented filters)
    "dedupe_repeats": True,     # general: an identical successful call adds nothing and wastes a step
    "ref_round": False,         # specific-risk: reads tickets named inside tickets, tuned on one dependency question
    "candidate_round": False,   # specific-risk: reads every search hit for 'when' questions, tuned on one conflict question
    "not_found_trim": False,    # specific-risk: cuts tangents after "not found", tuned on one missing-ticket question
}


def resolve_heuristics(env=None) -> dict[str, bool]:
    env = os.environ if env is None else env
    flags = dict(HEURISTIC_DEFAULTS)
    for var, value in (("AGENT_HEURISTICS_ON", True), ("AGENT_HEURISTICS_OFF", False)):
        for name in (n.strip() for n in (env.get(var) or "").split(",") if n.strip()):
            if name not in flags:
                raise ValueError(f"{var}: unknown heuristic {name!r}; known: {sorted(flags)}")
            flags[name] = value
    return flags


HEURISTICS = resolve_heuristics()
