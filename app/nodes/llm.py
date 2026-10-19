"""Thin Ollama wrapper so nodes never touch the SDK directly (and tests can fake it).

All model names and settings come from app/config.py.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app import config
from app.nodes import settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class LLMReply:
    content: str = ""
    # Native tool calls, normalised to [{"name": str, "arguments": dict | str}]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


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

    async def chat(
        self,
        job: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        format: dict | None = None,
    ) -> LLMReply:
        import ollama  # imported lazily so tests / tooling don't need a server

        client = ollama.AsyncClient(host=config.OLLAMA_BASE_URL, timeout=settings.get("LLM_TIMEOUT_S"))
        think = getattr(config, "THINKING", {}).get(job, False)
        resp = await client.chat(
            model=config.MODELS[job],
            messages=messages,
            tools=tools,
            format=format,
            think=think,
            stream=False,
            options={"num_ctx": config.NUM_CTX, "temperature": config.TEMPERATURE},
        )
        msg = resp.message
        calls = []
        for tc in msg.tool_calls or []:
            args = tc.function.arguments
            calls.append({"name": tc.function.name, "arguments": dict(args) if args else {}})
        return LLMReply(content=_THINK_RE.sub("", msg.content or "").strip(), tool_calls=calls)
