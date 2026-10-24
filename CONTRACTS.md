# CONTRACTS (frozen on Day 0; Phase 2 amendments in section 12)
Change only if **all four** (A, B, C, D) agree. Open a PR touching this file and get 4 approvals.
Phase 1 had three owners; D joins in Phase 2. Sections 1-9 are the Phase 1 contract as amended by section 12;
sections 10-12 are new.

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
Notion pages and emails have their own schemas, owned by A in `data/WORLD.md`. The only part fixed here is the
ID rule in section 10.

## 2) "Today" is frozen
`TODAY = "2026-06-30"` lives in `app/config.py`.
"Overdue" means `due_date < TODAY and status != Done`. Never use the real system date.

## 3) MCP tools (all read-only; every result item includes its "id")
Tool names are unique across servers (Day 0 decision 2). Jira names are unchanged.
```
search_tickets(keyword: str, status: str|None, label: str|None, overdue: bool|None)
  -> {"count": n, "results": [{id, title, status, assignee, due_date, priority}]}
get_ticket(ticket_id: str)
  -> full ticket object, or {"error": "not_found", "id": "<id>"}
list_overdue()
  -> {"count": n, "results": [{id, title, status, assignee, due_date, days_overdue}]}
```
New in Phase 2. Day 0 fixes the NAMES, the read-only rule, the ID rule and "keyword search with section-level
snippets" (decision 7). The shapes below are the proposal D confirms in review; D may adjust field names in the
PR that adds the server, and that PR must update this block.
```
search_pages(keyword: str)
  -> {"count": n, "results": [{id, title, section, snippet}]}          # one row per matching section
get_page(page_id: str)
  -> full page object, or {"error": "not_found", "id": "<id>"}
search_emails(keyword: str)
  -> {"count": n, "results": [{id, subject, from, date, thread_id, snippet}]}
get_email(email_id: str)
  -> full email object, or {"error": "not_found", "id": "<id>"}
get_thread(thread_id: str)
  -> {"thread_id": "<id>", "emails": [{id, subject, from, date}]}, or {"error": "not_found", "id": "<id>"}
```
Every server must NEVER print to stdout (corrupts the stdio protocol). Log to stderr.

## 4) Agent interface (`app/agent.py`)
`run_agent(question: str) -> AgentResult`
```
AgentResult = {
  "answer": str,
  "citations": [str],                  # source item IDs: ATL-, NTN- or EML- (section 10)
  "status": "ok | loop_cap | invalid_output | error",
  "trace_id": str | None,
  "tool_calls": [{"name": str, "args": dict, "latency_ms": int}],
  "latency_ms": int,
  # OPTIONAL (Phase 2, shapes agreed with B). Absent means "not reported"; every reader MUST tolerate absence.
  "plan": [{"id": "sq1", "question": str, "source": str, "reason": str,         # reason = the planner's
            "status": "resolved | unresolved | unavailable | not_run",
            "outcome": str,             # short code: model, no_records, budget_exhausted, source_down, ...
            "steps": int, "attempts": 1|2, "rewritten": str|None, "capped": bool, "unavailable": [str]}],
  "sources": [str],                     # sources that returned at least one record, in order of first use
  "token_usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int, "calls": int,
                  "by_job": {"<job>": {"prompt_tokens": int, "completion_tokens": int, "calls": int}}}
}
```
- `plan` and `sources` appear only when `PLANNER_ENABLED` is true; `token_usage` appears whenever the model reported
  counts (absent, not zero, when it did not). Total tool rounds = the sum of `plan[].steps`.
- A planner failure is not a status: after one retry the planner returns a single sub-question over all sources
  (Phase 1 behaviour) whose `reason` starts with `planner fallback`, and the run records a `planner_fallback:*` warning.
  `source` is `all` for that fallback sub-question.
- `sub-question status "unavailable"` means that source's server failed or returned `source_unavailable`; the run
  continues with the other sources (decision 6) and the answer must say "source unavailable".
- `status` is `loop_cap` only when `MAX_TOTAL_STEPS` left a sub-question unrun or unrewritten. A sub-question that
  hits `MAX_AGENT_STEPS` is marked `unresolved` and the run continues. "Not enough evidence" is a normal `ok` answer.
- Tracing fills `trace_id` and, when the model layer reports usage (section 11), `token_usage`.

## 5) Answer key format (`evals/answer_key.json`, `evals/phase2_key.json`)
```json
[{"id": "Q1", "question": "...", "expected_answer": "...",
  "expected_citations": ["ATL-000"], "forbidden_citations": ["ATL-001"],
  "sources_required": ["jira", "notion"], "must_mention": ["2026-09-15"],
  "notes": "why this is the answer"}]
```
- `forbidden_citations` is OPTIONAL. A correct answer must NOT cite these IDs.
  Absent means an empty list, so existing questions behave as before.
- Every ID in `expected_citations` and `forbidden_citations` must be a real item ID in its own source's data,
  and the two lists must not overlap.
- `sources_required` is OPTIONAL (Phase 2). A list of source names from `config.SOURCES`. A run passes this check
  only if its citations include at least one ID from each listed source. Absent means no requirement.
  Every listed source must also appear among the prefixes of `expected_citations` (tests enforce).
- `must_mention` is OPTIONAL (Phase 2). A list of strings that must appear in the answer text
  (case-insensitive, whitespace-normalised). Each string must also appear in `expected_answer`, so the answer
  stays checkable (tests enforce). Absent means no requirement.
- Phase 1 keys use none of the new fields and are scored exactly as before.

## 6) Config keys (`app/config.py`)
`MODELS` (dict by job: agent, synthesizer, planner, evidence_check, rewrite, judge, embedding), `THINKING` (one flag per
generation job), `OLLAMA_BASE_URL`, `NUM_CTX`, `TEMPERATURE`, `TODAY`, `DATA_PATH` (+ `NOTION_DATA_PATH`,
`EMAIL_DATA_PATH`), plus every other tunable (see section 9). Phase 2 step and planner keys:
- `MAX_AGENT_STEPS` (=5) is the **per-sub-question** step cap. With the planner off the whole run is one
  sub-question, so it is also the whole-run cap (Phase 1 behaviour).
- `MAX_TOTAL_STEPS` (=12) caps tool rounds across all sub-questions and rewrites in one run. `MAX_SUBQUESTIONS` (=4)
  caps the plan length. `MAX_REWRITES_PER_SUBQUESTION` (=1) is "rewrite the query once"; 0 turns rewriting off.
  Jobs in `MODELS`: agent, synthesizer, planner, evidence_check, rewrite, judge, embedding.
- `PLANNER_ENABLED` (default False; env `PLANNER_ENABLED`). Read at call time, not import time, so the runner can
  switch it per result file. False must reproduce Phase 1 exactly.
- The initial values of the two new caps are a starting point. They may be changed only by evidence on the tuning
  key, never the held-out key.

## 7) Held-out keys (`evals/heldout_key.json`, `evals/phase2_heldout_key.json`)
- Same format as `answer_key.json`, written by A.
- They are NEVER used to tune prompts or code. They are only run to measure generalization.
- Run with `python -m evals.run_evals --heldout` (Phase 1) or `--phase2 --heldout`; results go to
  `evals/results/phase1_heldout.json`, `phase2_heldout.json` (planner on) and `phase2_heldout_flat.json`
  (planner off).
- Agent code must never read or reference them. **B and D never see `phase2_heldout_key.json` before M4.**
  Tests that read it must never print its wording in a failure message (they report key id and location only).
- Fix failures with general changes only. Report planner-on and planner-off honestly, including regressions.

## 8) Prompt hygiene
No item IDs, titles, people's names, labels or tags from the data, and no answer-key wording, may appear inside
prompts (`AGENT_SYSTEM`, `SYNTH_SYSTEM`, planner and evidence-check prompts, hints) or node logic. This now covers
all three sources: Jira tickets, Notion pages (titles, authors, labels) and emails (subjects, senders,
recipients). Examples use placeholders such as `ATL-000`, `NTN-000`, `EML-000`, `label-a`, `term-a`. Enforced by
`tests/test_prompt_hygiene.py`, which scans `app/agent.py`, `app/graph.py`, `app/nodes/*.py` and `app/sources.py`
(when present).
Graph-side heuristics must carry a `# HEURISTIC <name> [general|specific-risk]` note and a switch in
`config.HEURISTICS`.

## 9) Tunables
All tunables (timeouts, size caps, step and call limits, heuristic switches, fault-injection timing) live in
`app/config.py`. C owns the file; B and D send changes as a PR to C. No numeric constants or slice limits in
`app/nodes/`.

## 10) Source IDs (Phase 2)
| Source | Prefix | Example placeholder | ID shape |
|---|---|---|---|
| Jira | `ATL` | `ATL-000` | `ATL-` + digits |
| Notion | `NTN` | `NTN-000` | `NTN-` + digits |
| Email | `EML` | `EML-000` | `EML-` + digits |
`config.SOURCE_ID_PREFIXES` is the single registry. A citation's source is its prefix. The citation verifier
accepts an ID only if it exists in the data of the source its prefix names.
Reserved fixtures (prompt-injection and PII items, for later phases) are listed in `data/reserved_fixtures.json`
(A owns it). They must never appear in any key, as an ID or in text; tests enforce this for every source.

## 11) Tracing, token accounting and fault injection (agreed between B, C and D)
- **Run wrapper:** C's `app/runtime.py` wraps `run_agent` through `tracing.observe_run` (trace id, total latency,
  `logs/runs.jsonl`). B does NOT decorate `run_agent`. The wrapper also attaches `token_usage` from the run's
  token counter when the result has none.
- **Per node:** B decorates each node function with `@node_span("name")` from `app.tracing`.
- **Planner and evidence check:** B wraps each planner call in `tracing.stage_span("planner", ...)` and each
  evidence check in `tracing.stage_span("evidence_check", subquestion_index=..., subquestion=..., source=...)`.
  `tracing.annotate(**attrs)` adds attributes to the current span (e.g. the final outcome).
- **Model calls:** the agent uses the raw `ollama` client, so these are not auto-instrumented. `llm.py` wraps the
  request in `tracing.llm_span(job, model)` and reports usage with `span.set_usage(prompt_tokens, completion_tokens)`.
  The span records the counts as OpenInference token attributes and adds them to the run's token counter.
  Tracing failures never break a run.
- **Fault injection:** `FAULT_INJECT="notion:fail,email:timeout"` (sources from `config.SOURCES`, modes from
  `config.FAULT_MODES`). The logic is `app/faults.py` (C). D calls `await faults.inject(source, "startup")` before
  launching a source's server and `await faults.inject(source, "call")` inside the per-source call path, **inside**
  the existing call timeout so an injected hang trips the real timeout. `fail` raises `faults.SourceFault`; D
  turns it into the same "source unavailable" outcome as a real failure. With the variable unset nothing changes.

## 12) Phase 2 amendments (Day 0 decisions, all four sign off here)
| # | Decision | Where it lands |
|---|---|---|
| 1 | ID prefixes: ATL (Jira), NTN (Notion), EML (email) | section 10, `config.SOURCE_ID_PREFIXES` |
| 2 | Tool names unique across servers. Keep Jira names. New: `search_pages`, `get_page`, `search_emails`, `get_email`, `get_thread` | section 3 |
| 3 | `AgentResult` gains OPTIONAL `plan`, `sources`, `token_usage`; the runner tolerates their absence | section 4 |
| 4 | `MAX_AGENT_STEPS=5` becomes the per-sub-question cap; add `MAX_TOTAL_STEPS`, `MAX_SUBQUESTIONS` | section 6 |
| 5 | Answer key gains OPTIONAL `sources_required` and `must_mention` | section 5 |
| 6 | Partial availability: one failed source does not stop the run; record "source unavailable" (Phase 1 stopped the whole run) | section 4 (`sources`), section 11 |
| 7 | Keyword search for every source, with section-level snippets. Embeddings only if a measured Notion failure needs them | section 3 |
| 8 | Official result files are recorded on ONE machine and ONE model version | runner records machine and model fingerprint |

**Needed from D** (B builds against these until D's servers land): a registry of sources `{name, description, tools,
available}`; the tools of a failed server either absent from `tools` or returning an error starting `source_unavailable`;
`known_ids` as the union of every source's IDs (otherwise NTN-/EML- citations are flagged `unknown_ticket_id`).

Additions made while writing these amendments (each needs the same four approvals): `app/faults.py` and the
`FAULT_INJECT` switch (C4), the token counter and span helpers in `app/tracing.py` (C5), and
`data/reserved_fixtures.json` (section 10). Phase 2 result files: `phase2_flat.json` (planner off),
`phase2_planner.json`, `phase2_heldout.json`, plus `phase2_heldout_flat.json` so the held-out planner-on versus
planner-off comparison is recorded the same way as the tuning key.

**Sign-off** (tick when you approve the PR that merges this file; merge needs all four):
- [ ] A (data and keys)
- [ ] B (agent, graph, planner, synthesizer)
- [ ] C (config, runner, tracing, tests, docs)
- [ ] D (MCP servers, client, tool node, verifier, sources)

## Ownership
| Owner | Paths |
|---|---|
| A | `data/` (synthetic data, `WORLD.md`, generators, `PRIVATE_NOTES.md`, `reserved_fixtures.json`), `evals/answer_key.json`, `evals/heldout_key.json`, `evals/phase2_key.json`, `evals/phase2_heldout_key.json` |
| B | `app/agent.py`, `app/graph.py`, `app/nodes/agent_node.py`, `app/nodes/synthesizer.py`, `app/nodes/llm.py`, `app/nodes/state.py`, `app/nodes/debug.py`, NEW `planner.py`, `evidence_check.py`, `retrieve.py`, `scope.py` (in `app/nodes/`) |
| C | `app/config.py`, `app/cli.py`, `app/tracing.py`, `app/runtime.py`, `app/stub_agent.py`, NEW `app/faults.py`, `evals/run_evals.py`, NEW `evals/compare.py`, README, `docs/`, tests that police the rules (`tests/test_prompt_hygiene.py`, `tests/test_keys.py`, `tests/test_evals.py`, `tests/test_compare.py`, `tests/test_faults.py`, `tests/test_tracing.py`, `tests/test_contracts.py`, `tests/corpus.py`) |
| D | `app/mcp_servers/` (all), `app/nodes/mcp_client.py`, `app/nodes/tool_node.py`, `app/nodes/verify.py`, NEW `app/sources.py` |

Rules: one PR per task, the file owner reviews, `CONTRACTS.md` changes need 4 approvals, build against stubs and
fake LLMs until the real pieces land. `state.py`, `debug.py` and `stub_agent.py` were unassigned in the Day 0 table;
they keep their Phase 1 owners (B, B, C).
