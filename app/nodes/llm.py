"""Thin Ollama wrapper so nodes never touch the SDK directly (and tests can fake it).

All model names and settings come from app/config.py.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app import config, tracing

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class LLMReply:
    content: str = ""
    # Native tool calls, normalised to [{"name": str, "arguments": dict | str}]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Token counts Ollama reported for this call (0 when the model or a fake gives none).
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _summarize(by_job: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Per-job rows -> {"prompt_tokens", "completion_tokens", "total_tokens", "calls", "by_job"}."""
    rows = {job: {**row, "total_tokens": row["prompt_tokens"] + row["completion_tokens"]}
            for job, row in sorted(by_job.items())}
    prompt = sum(r["prompt_tokens"] for r in rows.values())
    completion = sum(r["completion_tokens"] for r in rows.values())
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "calls": sum(r["calls"] for r in rows.values()), "by_job": rows}


class UsageMeter:
    """Running token totals per job. One OllamaLLM owns one meter; the agent reads it once per run."""

    def __init__(self) -> None:
        self._by_job: dict[str, dict[str, int]] = {}

    def add(self, job: str, prompt: int, completion: int) -> None:
        row = self._by_job.setdefault(job, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        row["prompt_tokens"] += prompt
        row["completion_tokens"] += completion
        row["calls"] += 1

    def snapshot(self) -> dict[str, Any]:
        return _summarize(self._by_job)


def usage_since(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Tokens spent between two snapshots, in the snapshot's own shape (jobs with no new calls are left out)."""
    rows: dict[str, dict[str, int]] = {}
    for job, row in after.get("by_job", {}).items():
        old = before.get("by_job", {}).get(job, {})
        calls = row["calls"] - old.get("calls", 0)
        if calls > 0:
            rows[job] = {"prompt_tokens": row["prompt_tokens"] - old.get("prompt_tokens", 0),
                         "completion_tokens": row["completion_tokens"] - old.get("completion_tokens", 0),
                         "calls": calls}
    return _summarize(rows)


class LLM(Protocol):
    async def chat(
        self,
        job: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        format: dict | None = None,
    ) -> LLMReply: ...


class OllamaLLM:
    """qwen3:8b via Ollama. num_ctx is always set explicitly (Ollama's default silently truncates)."""

    def __init__(self) -> None:
        self.usage = UsageMeter()  # prompt/generation tokens per job, read by arun_agent

    async def chat(
        self,
        job: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        format: dict | None = None,
    ) -> LLMReply:
        import ollama  # imported lazily so tests / tooling don't need a server

        client = ollama.AsyncClient(host=config.OLLAMA_BASE_URL, timeout=config.LLM_TIMEOUT_S)
        think = getattr(config, "THINKING", {}).get(job, False)
        with tracing.llm_span(job, config.MODELS[job]) as span:  # C5: model-call span with token counts
            resp = await client.chat(
                model=config.MODELS[job],
                messages=messages,
                tools=tools,
                format=format,
                think=think,
                stream=False,
                options={"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE},
            )
            span.set_usage(getattr(resp, "prompt_eval_count", None), getattr(resp, "eval_count", None))
        msg = resp.message
        calls = []
        for tc in msg.tool_calls or []:
            args = tc.function.arguments
            calls.append({"name": tc.function.name, "arguments": dict(args) if args else {}})
        prompt = int(getattr(resp, "prompt_eval_count", None) or 0)
        completion = int(getattr(resp, "eval_count", None) or 0)
        self.usage.add(job, prompt, completion)
        return LLMReply(content=_THINK_RE.sub("", msg.content or "").strip(), tool_calls=calls,
                        prompt_tokens=prompt, completion_tokens=completion)
