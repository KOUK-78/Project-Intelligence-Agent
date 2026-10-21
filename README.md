# Project Intelligence Agent

[![tests](https://github.com/KOUK-78/Project-Intelligence-Agent/actions/workflows/test.yml/badge.svg)](https://github.com/KOUK-78/Project-Intelligence-Agent/actions/workflows/test.yml)

**Requires Python 3.12** (CI and the pinned dependencies are tested on 3.12).

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
| `--stub`                | TEST-ONLY fake agent, explicit opt-in (smoke test). There is no silent fallback: if the real agent fails to import, the CLI stops with an error |
| `AGENT_DEBUG=1`         | Decision log on stderr, one line per graph node |
| `JIRA_DATA_PATH=<file>` | Point the MCP server at a different ticket file |

```powershell
$env:AGENT_DEBUG="1"; python -m app ask "..." 2> debug.txt     # PowerShell
```

### 4. Run the answer key

```bash
python -m evals.run_evals                       # original key, grade each answer by hand: p / f / s
python -m evals.run_evals --auto --repeat 3     # no prompts, exit 1 if any question fails
python -m evals.run_evals --key evals/heldout_key.json --auto --repeat 3
```

| Flag                   | Meaning                                                                 |
| ---------------------- | ----------------------------------------------------------------------- |
| `--key <file>`         | Answer file to run (`answer_key.json` or `heldout_key.json`)            |
| `--auto`               | No prompts. A run passes only if status is `ok`, every expected citation is present, no `forbidden_citations` are cited, and every cited ID exists. Exit code 1 if any question fails |
| `--repeat N`           | Run each question N times; reports pass rate per question and overall   |
| `--min-pass-rate 0.67` | Per-question pass rate needed to count as passing (default 1.0)         |
| `--no-grade`           | Record mechanical checks only                                           |
| `--require-trace`      | Exit 1 if any run has no `trace_id`                                     |
| `--stub`               | Plumbing test only; writes `*.stub.json`, never the real result files   |

`--auto` is a citation-level check and does not read the answer text. Hand grading still judges wording.

Results go to separate files: `evals/results/phase1_baseline.json` (original key) and `evals/results/phase1_heldout.json` (held-out key).
The held-out key is never used to tune prompts or code (see `CONTRACTS.md`).
Per run it records: status, citations, expected citations found, forbidden citations hit, invalid IDs, latency and trace id.

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
