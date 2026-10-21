"""B-side tunables with safe defaults, so app/config.py (owned by C) can stay minimal.

If C later adds any of these names to config.py, the config value wins.
"""
from app import config

_DEFAULTS = {
    "MCP_CALL_TIMEOUT_S": 15,     # one tool call
    "MCP_STARTUP_TIMEOUT_S": 20,  # server launch + initialize + list_tools
    "LLM_TIMEOUT_S": 120,         # one Ollama request
    "MAX_TOOL_CHARS": 2500,       # cap on one tool result fed back to the model
    "MAX_CALLS_PER_STEP": 6,      # parallel tool calls allowed in one agent decision
}


def get(name: str):
    return getattr(config, name, _DEFAULTS[name])
