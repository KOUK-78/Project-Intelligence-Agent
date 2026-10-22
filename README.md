# Project Intelligence Agent

[![tests](https://github.com/KOUK-78/Project-Intelligence-Agent/actions/workflows/test.yml/badge.svg)](https://github.com/KOUK-78/Project-Intelligence-Agent/actions/workflows/test.yml)

**Requires Python 3.12** (CI and the pinned dependencies are tested on 3.12).

Answers multi-step project questions with **verified ticket citations**, every step traced.

**Phase 1: complete, with known limitations.** One vertical slice works end to end: a question goes in, the agent calls Jira tools over MCP, and a cited answer comes out, with every step traced in Phoenix.
Measured, not claimed: see [Results](#results) and [Known limitations](#known-limitations).

```
question -> LangGraph agent (qwen3:8b via Ollama) -> Jira MCP server (stdio) -> cited answer
                          \-> Arize Phoenix trace + logs/runs.jsonl
```

Frozen interfaces live in [`CONTRACTS.md`](CONTRACTS.md). Data is synthetic only (fictional company Northwind, project Atlas).

## Results

Recorded with tracing on, `qwen3:8b`, `--auto --repeat 3` (every run has a trace id). Raw data: `evals/results/phase1_baseline.json` and `evals/results/phase1_heldout.json`.

| Key | Questions passing (all 3 runs) | Runs passing | Latency median / worst |
|---|---|---|---|
| Original (`answer_key.json`, 10 questions) | 7 of 10 | 23 of 30 (76.7%) | 7.9 s / 16.4 s |
| Held-out (`heldout_key.json`, 10 questions) | 6 of 10 | 18 of 30 (60.0%) | 8.6 s / 15.7 s |

- The held-out key was never used to tune prompts or code, so the 60% is the honest generalization number. The gap to the original key is expected.
- The 8B model varies between runs. Three runs of the original key gave 73% to 80% of runs passing, so read these as +/- a few points.
- `--auto` is a citation-level check (status ok, expected citations present, no forbidden citations, no invalid IDs). It does not read the answer text. See limitations.
- Earlier tuning runs (heuristics on/off) are kept in `evals/results/history/`. They were recorded without tracing and are not the baseline.

### A trace in Phoenix

![A full run trace in Phoenix: MCP handshake, agent and tool steps, synthesizer, per-step timing](docs/phoenix_trace.png)

## Failing questions and causes

Causes are from reading the traces of the failing runs (`AGENT_DEBUG=1`). Runs failing = out of 3. Reasons are the ones the runner recorded.

| Key | Q | Question (short) | Runs failing | Recorded reason | Cause |
|---|---|---|---|---|---|
| Original | Q6 | When is the Atlas public launch? | 3 | missing `ATL-103`, `ATL-139` | Retrieval depth: the agent opens only the first search hit and states one date. The two launch tickets conflict and neither is cited. |
| Original | Q7 | Status of the Salesforce CRM integration? | 1 | forbidden `ATL-114` | Tangent after a not-found: the agent adds a related ticket it should not cite. The other 2 runs are clean. |
| Original | Q9 | Is the overdue privacy ticket late because of the payments API? | 3 | forbidden `ATL-112` | Premise check: the agent correctly says no, but cites the payments ticket it just ruled out. The key forbids that citation; whether that is too strict is for A to decide. |
| Held-out | H4 | Is the penetration test delayed because of the payments API? | 3 | missing `ATL-136`, forbidden `ATL-112` | The agent searches the suggested cause instead of the pen-test ticket, so it never cites it and cites the payments ticket. |
| Held-out | H5 | Which tickets are due on 2026-06-30, and are they overdue? | 3 | missing `ATL-116`; run 1 also forbidden `ATL-112`, `109`, `118`, `121`, `127`, `133` | Wrong tool: a due-date question is answered from the overdue list, so the ticket due that day is missed. Run 1 also lists the whole payments chain. |
| Held-out | H7 | Which overdue tickets are not caused by the payments API delay? | 3 | `status=loop_cap`, missing `ATL-109` | Step cap: the agent reads tickets one at a time and hits the 5-step limit before answering. |
| Held-out | H10 | How many tickets are labeled launch, and how many are still To Do? | 3 | missing `ATL-103`, `131`, `139`, `140` | Counts are right but no ticket is cited. A hand grader would pass it; the auto check cannot. |

Not covered by the table: `--auto` does not read answer text, so a wrong answer with correct citations passes.

## Known limitations

- **Over-answering.** On "is X caused by Y?" style questions the agent sometimes adds the related blocker and its downstream tickets. The extra citations trip the forbidden-citation check. This is the most common failure on both keys.
- **Conflicting sources.** When two tickets disagree (for example two launch dates), the agent can state one value without flagging the conflict.
- **Aggregate answers can lack citations.** A count or summary can be right in the text while citing no ticket, which the auto check marks as a failure. Hand grading would pass some of these.
- **Step cap.** Some multi-hop questions hit the 5-step cap and return an honest "could not finish" (`status=loop_cap`) instead of an answer.
- **Date edge cases.** "Due on a date" versus "overdue" can be confused at the boundary.
- **Citation-level scoring is approximate.** It can undercount (right answer, no citation) and overcount (right citation, wrong wording). LLM-judge scoring is planned for Phase 4.
- **Small and synthetic.** 45 tickets, one tool source (Jira), 10 + 10 questions. Results are indicative, not statistically strong.
- **Local model only.** Latency (about 8 s median) reflects `qwen3:8b` on an 8 GB GPU.

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
python -m evals.run_evals --heldout --auto --repeat 3
```

| Flag                   | Meaning                                                                 |
| ---------------------- | ----------------------------------------------------------------------- |
| `--key <file>`         | Answer file to run (default `answer_key.json`)                          |
| `--heldout`            | Run `heldout_key.json` (measurement only; cannot be combined with `--key`) |
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
data/
  synthetic/jira_tickets.json                 synthetic Northwind / Atlas tickets (A)
  generate_jira.py, PRIVATE_NOTES.md          dataset generator and design notes (A)
evals/
  run_evals.py                                eval runner (C)
  answer_key.json, heldout_key.json           answer files (A)
  results/                                    phase1_baseline.json, phase1_heldout.json, history/
docs/                                         SETUP.md, phoenix_trace.png
.github/workflows/test.yml                    CI (pytest)
tests/
```

## Rules of the repo

- Model names live only in `app/config.py`.
- "Today" is frozen: `TODAY = "2026-06-30"`. Never use the system date.
- The MCP server must never print to stdout (it carries the protocol). Log to stderr.
- One branch per person, merge by pull request. Dependencies are pinned in `requirements.txt`.
- Out of scope until later phases: planner, memory, guardrails, Notion, email, GitHub, FastAPI, UI, automated judging.
