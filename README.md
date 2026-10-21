# Project Intelligence Agent

Answers multi-step project questions with **verified ticket citations**, every step traced.

**Phase 1 (done):** one vertical slice. A question goes in, the agent calls a Jira tool over MCP, and a cited answer comes out.
Baseline: **10 of 10 correct** on the hand-graded answer key (`evals/results/phase1_baseline.json`).

```
question -> LangGraph agent (qwen3:8b via Ollama) -> Jira MCP server (stdio) -> cited answer
                          \-> Arize Phoenix trace + logs/runs.jsonl
```

Frozen interfaces live in [`CONTRACTS.md`](CONTRACTS.md). Data is synthetic only (fictional company Northwind, project Atlas).

## Quickstart

### 1. Clone and install

```bash
git clone https://github.com/KOUK-78/Project-Intelligence-Agent.git
cd Project-Intelligence-Agent
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

`.env` is gitignored. The defaults work; fill values only to override them.

### 2. Set up Ollama

Full guide: [`docs/SETUP.md`](docs/SETUP.md). The short version:

```powershell
ollama pull qwen3:8b
ollama pull nomic-embed-text
ollama ps        # PROCESSOR must say 100% GPU
```

Keep `NUM_CTX = 8192` on 8 GB cards and close heavy apps first.

### 3. Run the CLI

```bash
python -m app ask "What is blocking Project Atlas?"
```

Prints answer, citations, status, trace id, latency and tool calls.
The agent launches the Jira MCP server itself as a subprocess; you do not start it.

| Option                  | Use                                             |
| ----------------------- | ----------------------------------------------- |
| `--stub`                | Fake agent, no Ollama needed (smoke test)       |
| `AGENT_DEBUG=1`         | Decision log on stderr, one line per graph node |
| `JIRA_DATA_PATH=<file>` | Point the MCP server at a different ticket file |

```powershell
$env:AGENT_DEBUG="1"; python -m app ask "..." 2> debug.txt     # PowerShell
```

### 4. Run the answer key

```bash
python -m evals.run_evals              # grade each answer by hand: p / f / s
python -m evals.run_evals --no-grade   # mechanical checks only
```

Each question prints expected vs. actual side by side. You mark pass, fail or skip.
Output: a summary table, `SCORE: X of N correct`, a failure list, and `evals/results/phase1_baseline.json`.

Per question it records: status, citations returned, whether all expected citations were found, whether any cited ID does not exist in the data, and latency.
A question also counts as a failure if its status is not `ok`, a citation is missing, or a cited ID is invalid.

### 5. View traces

```bash
# second terminal
export PHOENIX_WORKING_DIR="$PWD/.phoenix"     # PowerShell: $env:PHOENIX_WORKING_DIR="$PWD\.phoenix"
phoenix serve
```

Open http://localhost:6006. Each run shows the question, every model call, every tool call (inputs and outputs) and timing.
If Phoenix is not running, runs still work and a warning goes to stderr.

Offline fallback: every run is appended to `logs/runs.jsonl` (question, answer, citations, status, tool_calls, latency_ms, trace_id).

### 6. Tests

```bash
pytest
```

## Repo layout

```
app/
  config.py, cli.py, tracing.py, runtime.py   platform (C)
  agent.py, graph.py, nodes/                  agent (B)
  mcp_servers/jira_server.py                  Jira MCP tools (A)
data/synthetic/                               jira_tickets.json (A)
evals/                                        run_evals.py (C), answer_key.json (A), results/
docs/SETUP.md                                 Ollama + Phoenix setup
tests/
```

## Rules of the repo

- Model names live only in `app/config.py`.
- "Today" is frozen: `TODAY = "2026-06-30"`. Never use the system date.
- The MCP server must never print to stdout (it carries the protocol). Log to stderr.
- One branch per person, merge by pull request. Dependencies are pinned in `requirements.txt`.
- Out of scope until later phases: planner, memory, guardrails, Notion, email, GitHub, FastAPI, UI, automated judging.
