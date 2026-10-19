# Project Intelligence Agent

Answers multi-step project questions with verified ticket citations, every step traced.
**Phase 1:** one vertical slice (question -> Jira tool via MCP -> cited answer). See `CONTRACTS.md` for the frozen interfaces.

## Phase 1 quickstart

### 1. Install
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # Windows: copy .env.example .env
```

### 2. Set up Ollama
Follow [`docs/SETUP.md`](docs/SETUP.md) (pull `qwen3:8b`, set env vars, check `ollama ps` shows 100% GPU).

### 3. Run the CLI
```bash
python -m app ask "What is blocking Project Atlas?"
python -m app ask --stub "..."     # fake agent, no Ollama needed
```
Prints answer, citations, status, trace id, latency and tool calls.
Until `app/agent.py` exists the CLI falls back to the stub automatically.

### 4. Run the answer key (first score)
```bash
python -m evals.run_evals            # grade each answer by hand: p / f / s
python -m evals.run_evals --no-grade # mechanical checks only
```
Writes `evals/results/phase1_baseline.json` and prints a table, e.g. `SCORE: 7 of 10 correct`, plus a failure list.
Per question: status, citations, all expected citations found, any invalid cited ID, latency.

### 5. View traces
```bash
phoenix serve        # then open http://localhost:6006
```
Each run shows the question, every model call, every tool call (inputs/outputs) and timing.
Offline fallback: every run is also appended to `logs/runs.jsonl`.

### Tests
```bash
pytest
```

## Layout
```
app/           config, cli, tracing, runtime (C) | agent, nodes, graph (B) | mcp_servers (A)
data/synthetic Northwind / Atlas tickets (A)
evals/         run_evals.py (C), answer_key.json (A), results/
docs/          SETUP.md
```
Branching: one branch per person, merge by pull request. Dependencies are pinned in `requirements.txt`.
