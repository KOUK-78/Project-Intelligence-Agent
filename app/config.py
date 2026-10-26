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
    "planner": "qwen3:8b",        # Phase 2: split the question into source-tagged sub-questions
    "evidence_check": "qwen3:8b", # Phase 2: is the evidence for a sub-question enough (yes/no)
    "rewrite": "qwen3:8b",        # Phase 2: rewrite a sub-question once after an insufficient check
}
# Thinking off for every Phase 2 job too (pass as reasoning=False to ChatOllama)
THINKING = {"agent": False, "synthesizer": False, "planner": False, "evidence_check": False, "rewrite": False}

# --- Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
NUM_CTX = 8192            # keep at 8192 on 8 GB cards
TEMPERATURE = 0.1

# --- Reproducibility: never use the real system date
TODAY = "2026-06-30"

# --- Agent
# MAX_AGENT_STEPS is the PER-SUB-QUESTION cap (CONTRACTS.md section 6). With the planner off the whole run is
# one sub-question, so it is also the whole-run cap and Phase 1 behaviour is unchanged.
MAX_AGENT_STEPS = 5
PROJECT_NAME = "Atlas"             # the project every ticket belongs to; never a useful search term
AGENT_DEBUG = (os.getenv("AGENT_DEBUG") or "0").lower() not in ("0", "false", "no")  # per-node decision log on stderr
STATUS_VALUES = ("To Do", "In Progress", "Blocked", "Done")  # CONTRACTS.md section 1
MAX_CALLS_PER_STEP = 6             # tool calls the model may issue in one decision
MAX_REF_TICKETS = 4                # tickets the graph reads in its follow-the-references round
MAX_CANDIDATE_TICKETS = 6          # tickets the graph reads in its read-every-candidate round
GRAPH_RECURSION_LIMIT = 40         # LangGraph backstop only; MAX_AGENT_STEPS is the real cap

# --- Phase 2: planner + scoped retrieval. PLANNER_ENABLED=false reproduces Phase 1 exactly.
PLANNER_ENABLED = (os.getenv("PLANNER_ENABLED") or "0").lower() not in ("0", "false", "no")
# MAX_AGENT_STEPS (above) is now the cap PER SUB-QUESTION ATTEMPT. These two bound the whole run.
# "Flat" = ONE retrieval loop over ALL sources with NO planner: the fair baseline for the planner. D built it: it is
# the planner-off run with ENABLED_SOURCES=all (see resolve_enabled_sources below); `evals.run_evals --flat` sets that
# for the run. With the planner off and ENABLED_SOURCES unset the agent is the Phase 1 graph (Jira only), which is
# NOT a flat baseline. Set this False to make `--flat` refuse to run, so nothing can be mislabelled "flat".
FLAT_MODE_IMPLEMENTED = True
MAX_SUBQUESTIONS = 4               # the planner's plan is cut to this many sub-questions
MAX_TOTAL_STEPS = 12               # tool rounds across ALL sub-questions and rewrites
MAX_REWRITES_PER_SUBQUESTION = 1   # "rewrite the query once"; 0 turns rewriting off
SUBQUESTION_CHARS = 300            # longest sub-question / rewritten query we keep
REASON_CHARS = 200                 # longest planner "reason" we keep
EVIDENCE_CHECK_CHARS = 4000        # evidence shown to the yes/no check (smaller than the synthesizer's)

# Source registry: one name and one citation-ID prefix per source (Day 0 decision 1).
SOURCES = ("jira", "notion", "email")
SOURCE_ID_PREFIXES = {"jira": "ATL", "notion": "NTN", "email": "EML"}
# IDs that look like item IDs but are NOT citable (CONTRACTS.md section 3: a thread ID is not a citable item).
# The verifier and the bracket logic leave them alone: no warning, no "[unverified: ...]", no bracketing.
NON_CITABLE_ID_PREFIXES = ("THR",)

# --- Source switch (D). ENABLED_SOURCES chooses which sources a run can reach:
#   unset, planner off  -> "jira" only: Phase 1 exactly (one server, one flat loop). The regression mode.
#   unset, planner on   -> every source (jira, notion, email) with the planner.
#   "all" or "jira,notion,..." with the planner OFF -> FLAT MODE: those sources behind one router and ONE flat
#                          retrieval loop over all their tools (the M2 baseline, to compare against the planner).
#   a list with the planner on -> the planner plans over just those sources.
def resolve_enabled_sources(env=None, planner: bool | None = None) -> tuple[str, ...]:
    """Parse ENABLED_SOURCES into source names in registry order. Unknown names or an empty list raise."""
    env = os.environ if env is None else env
    planner = PLANNER_ENABLED if planner is None else planner
    raw = (env.get("ENABLED_SOURCES") or "").strip().lower()
    if not raw:
        return SOURCES if planner else ("jira",)
    if raw == "all":
        return SOURCES
    names = [p.strip() for p in raw.split(",") if p.strip()]
    bad = [n for n in names if n not in SOURCES]
    if bad or not names:
        raise ValueError(f"ENABLED_SOURCES: bad value {raw!r}; use 'all' or a comma list of {list(SOURCES)}")
    return tuple(n for n in SOURCES if n in names)


# --- Fault injection (C4). FAULT_INJECT="notion:fail,email:timeout" makes that source misbehave.
#   fail    = the source's server cannot start and every call errors   -> "source unavailable"
#   timeout = every call hangs for FAULT_TIMEOUT_SLEEP_S, so the caller's own timeout must trip first
# Hook points are described in CONTRACTS.md section 11; the logic lives in app/faults.py.
FAULT_MODES = ("fail", "timeout")


def resolve_faults(env=None) -> dict[str, str]:
    """Parse FAULT_INJECT into {source: mode}. Unknown sources or modes raise, like resolve_heuristics."""
    env = os.environ if env is None else env
    faults: dict[str, str] = {}
    for item in (p.strip() for p in (env.get("FAULT_INJECT") or "").split(",") if p.strip()):
        source, sep, mode = (s.strip().lower() for s in item.partition(":"))
        if not sep or source not in SOURCES or mode not in FAULT_MODES:
            raise ValueError(f"FAULT_INJECT: bad entry {item!r}; use <source>:<mode> with source in {list(SOURCES)} "
                             f"and mode in {list(FAULT_MODES)}")
        faults[source] = mode
    return faults


# --- Timeouts (seconds)
MCP_CALL_TIMEOUT_S = 15            # one tool call
MCP_STARTUP_TIMEOUT_S = 20         # server launch + initialize + list_tools
LLM_TIMEOUT_S = 120                # one Ollama request
FAULT_TIMEOUT_SLEEP_S = MCP_CALL_TIMEOUT_S * 2   # injected 'timeout' hang; always longer than the call timeout
ENV_PROBE_TIMEOUT_S = 3            # git / Ollama probes that describe the machine in result files

# --- Size caps (characters); keep prompts short, num_ctx is 8192 tokens
MAX_TOOL_CHARS = 2500              # one tool result fed back to the agent
EVIDENCE_ITEM_CHARS = 2000         # one tool result shown to the synthesizer
EVIDENCE_TOTAL_CHARS = 9000        # all evidence shown to the synthesizer
EVIDENCE_TOTAL_CHARS_PLANNED = 12000   # same, for the planned run's grouped evidence (several sources share it)
ERROR_TEXT_CHARS = 300             # error text kept in results and logs
SNIPPET_CHARS = 120                # short excerpt of a bad model/tool output
SPAN_TEXT_CHARS = 200              # longest text value stored as a trace span attribute

# --- Paths
DATA_PATH = ROOT / "data" / "synthetic" / "jira_tickets.json"
# File names as delivered by A (data/WORLD.md); NOTION_DATA_PATH / EMAIL_DATA_PATH override them.
NOTION_DATA_PATH = Path(os.getenv("NOTION_DATA_PATH") or ROOT / "data" / "synthetic" / "notion_pages.json")
EMAIL_DATA_PATH = Path(os.getenv("EMAIL_DATA_PATH") or ROOT / "data" / "synthetic" / "email_messages.json")
DATA_PATHS = {"jira": DATA_PATH, "notion": NOTION_DATA_PATH, "email": EMAIL_DATA_PATH}
RESERVED_FIXTURES_PATH = ROOT / "data" / "reserved_fixtures.json"  # A lists every injection/PII fixture here
LOGS_DIR = ROOT / "logs"
RUNS_LOG = LOGS_DIR / "runs.jsonl"
ANSWER_KEY_PATH = ROOT / "evals" / "answer_key.json"
HELDOUT_KEY_PATH = ROOT / "evals" / "heldout_key.json"  # measurement only, never tuned against (CONTRACTS.md 7)
PHASE2_KEY_PATH = ROOT / "evals" / "phase2_key.json"
# Measurement only (CONTRACTS.md 7). The Phase 2 held-out key is NOT in the repo: it lives on the measurement machine and
# this variable points at it. The default path is git-ignored, so a copy dropped there is never committed.
PHASE2_HELDOUT_KEY_PATH = Path(os.getenv("PHASE2_HELDOUT_KEY_PATH") or ROOT / "evals" / "phase2_heldout_key.json")
RESULTS_DIR = ROOT / "evals" / "results"

# --- Regression floor (C3): a Phase 1 key must still pass at least this many runs, as (runs_passed, runs_total).
# These are the bottom of the recorded Phase 1 ranges (original 22-24/30, held-out 15-18/30).
REGRESSION_FLOORS = {"answer_key.json": (22, 30), "heldout_key.json": (15, 30)}

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
    "status_sanitize": True,    # general: a status filter must be exactly one valid status; lists and invented values are dropped
    "partial_on_cap": False,    # general idea, but held-out run showed partial answers can be confidently wrong; OFF until it helps
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
