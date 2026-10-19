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

## Troubleshooting
| Symptom | Fix |
|---|---|
| `ollama ps` shows CPU split | Close apps, restart Ollama, confirm `NUM_CTX=8192` |
| `[tracing] Phoenix tracing disabled` | Start `phoenix serve`; runs still work and still log to `logs/runs.jsonl` |
| MCP server breaks the protocol | Something printed to stdout. Log to stderr only |
