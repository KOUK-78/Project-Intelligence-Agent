"""Scripted fake LLM: lets us test the graph without Ollama or a GPU."""
import json
from typing import Callable

from app.nodes.llm import LLMReply


def tool_reply(name: str, **args) -> LLMReply:
    return LLMReply(content="", tool_calls=[{"name": name, "arguments": args}])


def text_reply(text: str = "DONE") -> LLMReply:
    return LLMReply(content=text)


def synth_reply(answer: str, citations: list[str]) -> LLMReply:
    return LLMReply(content=json.dumps({"answer": answer, "citations": citations}))


class FakeLLM:
    """script[job] is a list of replies (consumed in order; the last one repeats) or a callable."""

    def __init__(self, **script: list[LLMReply] | Callable[[int], LLMReply]) -> None:
        self.script = script
        self.calls: dict[str, list[list[dict]]] = {}

    async def chat(self, job, messages, *, tools=None, format=None) -> LLMReply:
        history = self.calls.setdefault(job, [])
        history.append(messages)
        n = len(history) - 1
        plan = self.script[job]
        if callable(plan):
            return plan(n)
        return plan[min(n, len(plan) - 1)]

    def count(self, job: str) -> int:
        return len(self.calls.get(job, []))
