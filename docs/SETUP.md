# Setup (Windows, 8 GB VRAM GPU)

## 1. Install Ollama and pull models
```powershell
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

## 2. Set Ollama environment variables (PowerShell, once)
```powershell
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
setx OLLAMA_MAX_LOADED_MODELS 1
```
**Quit Ollama from the system tray and start it again**, or the new values are not picked up.

## 3. Confirm the GPU is used
```powershell
ollama run qwen3:8b "say hi"
ollama ps
```
The `PROCESSOR` column must say **100% GPU**. If it shows a CPU split, the model is spilling out of VRAM:
- Close heavy apps first (browsers with many tabs, games, video editors).
- Keep `NUM_CTX = 8192` in `app/config.py`. Do not raise it on 8 GB cards.

## 4. Python environment
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
`.env` is gitignored. Never commit it. Fill values only if you need to override defaults.

## 5. Start Phoenix (tracing UI), in a second terminal
```powershell
$env:PHOENIX_WORKING_DIR = "$PWD\.phoenix"
phoenix serve
```
Open http://localhost:6006. Traces are stored in a local SQLite file under `.phoenix/`.

## 6. Smoke test
```powershell
python -m app ask --stub "What is blocking Project Atlas?"
```
Works without Ollama or Phoenix. You should see an answer, citations, status, latency and tool calls, and a new line in `logs/runs.jsonl`.

## 7. Generate the data
```powershell
python data/generate_jira.py          # Jira tickets -> data/synthetic/jira_tickets.json
```
Phase 2 adds Notion pages and emails. Their generators, output files and schema are owned by A and described in `data/WORLD.md`; run them from the repo root the same way. The paths the code reads are `NOTION_DATA_PATH` and `EMAIL_DATA_PATH` in `app/config.py` (defaults `data/synthetic/notion_pages.json` and `data/synthetic/emails.json`; set the environment variables to point elsewhere). Until those files exist, Phase 2 citations count as invalid in the runner and the policing tests for those sources simply have nothing to check.

## 8. Official measurement runs
Result files recorded for the README (milestones M2 and M4) must come from ONE machine, ONE model version, with Phoenix on:
```powershell
ollama show qwen3:8b            # note the digest; the runner also records it
$env:PHOENIX_WORKING_DIR = "$PWD\.phoenix"; phoenix serve     # second terminal
python -m evals.run_evals --phase2 --flat --auto --repeat 3 --require-trace   # only once flat mode exists; it refuses otherwise
python -m evals.run_evals --phase2 --planner    --auto --repeat 3 --require-trace
python -m evals.compare evals/results/phase2_flat.json evals/results/phase2_planner.json
python -m evals.run_evals --auto --repeat 3 --planner --require-trace   # Phase 1 regression, planner on
python -m evals.compare evals/results/phase1_baseline.json evals/results/phase1_baseline_planner.json
```
Close other GPU apps first, keep `NUM_CTX = 8192`, and do not change `config.py` between the paired runs. The held-out key (`--phase2 --heldout`) is run only at M4, once, and never used to tune anything. `compare` warns if two files differ in machine, model digest or repeat count; do not publish a comparison that warns.

## Fault injection
Make a source fail or hang to test the "source unavailable" path (used by tests now and by the Phase 4 tool-failure evals later):
```powershell
$env:FAULT_INJECT = "notion:fail"                  # notion cannot start and every call errors
$env:FAULT_INJECT = "email:timeout"                # every email call hangs until the call timeout trips
$env:FAULT_INJECT = "notion:fail,email:timeout"    # several at once
Remove-Item Env:FAULT_INJECT                       # back to normal
```
Sources are `jira`, `notion`, `email`; modes are `fail` and `timeout`. A bad entry stops the run with an error naming the valid values. The runner prints a warning and records the setting in the result file, and `compare` warns about it: a fault run is a test, never a measurement. The hook points D wires in are in `CONTRACTS.md` section 11.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `ollama ps` shows CPU split | Close apps, restart Ollama, confirm `NUM_CTX=8192` |
| `[tracing] Phoenix tracing disabled` | Start `phoenix serve`; runs still work and still log to `logs/runs.jsonl` |
| MCP server breaks the protocol | Something printed to stdout. Log to stderr only |
| `FAULT_INJECT: bad entry` | Use `<source>:<mode>`, source in jira/notion/email, mode in fail/timeout |
| `compare` exits 2 | The two files are for different keys, unreadable, or one is a stub run |
| `compare` exits 1 | REGRESSION: the candidate passes fewer runs than the Phase 1 floor (22/30 original, 15/30 held-out) |
