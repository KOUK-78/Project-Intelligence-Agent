# CONTRACTS (frozen on Day 0)
Change only if **all three** (A, B, C) agree. Open a PR touching this file and get 3 approvals.

## 1) Ticket schema (JSON)
```json
{
  "id": "ATL-101",
  "title": "string",
  "description": "string",
  "status": "To Do | In Progress | Blocked | Done",
  "assignee": "string",
  "created_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD",
  "priority": "Low | Medium | High | Critical",
  "labels": ["string"],
  "comments": [{"author": "string", "date": "YYYY-MM-DD", "text": "string"}]
}
```

## 2) "Today" is frozen
`TODAY = "2026-06-30"` lives in `app/config.py`.
"Overdue" means `due_date < TODAY and status != Done`. Never use the real system date.

## 3) MCP tools (all read-only; every result includes the ticket "id")
```
search_tickets(keyword: str, status: str|None, label: str|None, overdue: bool|None)
  -> {"count": n, "results": [{id, title, status, assignee, due_date, priority}]}
get_ticket(ticket_id: str)
  -> full ticket object, or {"error": "not_found", "id": "<id>"}
list_overdue()
  -> {"count": n, "results": [{id, title, status, assignee, due_date, days_overdue}]}
```
The server must NEVER print to stdout (corrupts the stdio protocol). Log to stderr.

## 4) Agent interface (`app/agent.py`)
`run_agent(question: str) -> AgentResult`
```
AgentResult = {
  "answer": str,
  "citations": [str],                  # ticket IDs
  "status": "ok | loop_cap | invalid_output | error",
  "trace_id": str | None,
  "tool_calls": [{"name": str, "args": dict, "latency_ms": int}],
  "latency_ms": int
}
```

## 5) Answer key format (`evals/answer_key.json`)
```json
[{"id": "Q1", "question": "...", "expected_answer": "...",
  "expected_citations": ["ATL-000"], "forbidden_citations": ["ATL-001"],
  "notes": "why this is the answer"}]
```
- `forbidden_citations` is OPTIONAL. A correct answer must NOT cite these IDs.
  Absent means an empty list, so existing questions behave as before.
- Every ID in `expected_citations` and `forbidden_citations` must be a real ticket ID, and the two lists must not overlap.

## 6) Config keys (`app/config.py`)
`MODELS` (dict by job), `OLLAMA_BASE_URL`, `NUM_CTX`, `TEMPERATURE`, `TODAY`,
`MAX_AGENT_STEPS` (=5), `DATA_PATH`, plus every other tunable (see section 9).

## 7) Held-out key (`evals/heldout_key.json`)
- Same format as `answer_key.json`, written by A.
- It is NEVER used to tune prompts or code. It is only run to measure generalization.
- Run with `python -m evals.run_evals --heldout`; results go to `evals/results/phase1_heldout.json`.
- Agent code must never read or reference it.

## 8) Prompt hygiene
No ticket IDs, ticket titles, people's names from the data, or answer-key wording may appear inside
prompts (`AGENT_SYSTEM`, `SYNTH_SYSTEM`, hints) or node logic. Examples use placeholders such as
`ATL-000`, `label-a`, `term-a`. Enforced by `tests/test_prompt_hygiene.py`.
Graph-side heuristics must carry a `# HEURISTIC <name> [general|specific-risk]` note and a switch in
`config.HEURISTICS`.

## 9) Tunables
All tunables (timeouts, size caps, step and call limits, heuristic switches) live in `app/config.py`.
C owns the file; B sends changes as a PR to C. No numeric constants or slice limits in `app/nodes/`.

## Ownership
| Owner | Paths |
|---|---|
| A | `data/` (synthetic data, `generate_jira.py`, `PRIVATE_NOTES.md`), `app/mcp_servers/`, `evals/answer_key.json`, `evals/heldout_key.json` |
| B | `app/agent.py`, `app/nodes/`, `app/graph.py` |
| C | `app/config.py`, `app/cli.py`, `app/tracing.py`, `app/runtime.py`, `app/stub_agent.py`, `evals/run_evals.py`, README, `docs/` |

## Tracing hook points (agreed between B and C)
- **Run wrapper:** C's `app/runtime.py` wraps `run_agent` (trace id, total latency, `logs/runs.jsonl`). B does NOT decorate `run_agent`.
- **Per node:** B decorates each node function with `@node_span("name")` from `app.tracing`.
- **Model and tool calls:** the agent uses the raw `ollama` client, so these are not auto-instrumented; only node spans appear in Phoenix.