"""Fault injection: make a chosen source fail or hang. OWNER: C. Hook points: CONTRACTS.md section 11.

    FAULT_INJECT="notion:fail"                 notion cannot start and every call errors
    FAULT_INJECT="email:timeout"               every email call hangs until the caller's timeout trips
    FAULT_INJECT="notion:fail,email:timeout"   several at once

Used by tests now and by the Phase 4 tool-failure evals later. With the variable unset every function here
is a no-op, so normal runs are untouched.

Two ways to use it:
  * D's per-source call path awaits `inject(source, "startup" | "call")` (inside the call timeout).
  * `FaultyClient(client, source)` wraps any MCP-style client for tests that do not go through D's code.
"""
import os
from typing import Any

import anyio

from app import config

STAGES = ("startup", "call")


class SourceFault(RuntimeError):
    """Raised by an injected 'fail' fault. The caller treats it like any real source failure."""

    def __init__(self, source: str, stage: str) -> None:
        super().__init__(f"injected fault: {source} {stage} failed")
        self.source, self.stage = source, stage


def active(env=None) -> dict[str, str]:
    """{source: mode} currently injected. Read on every call so a test can change the variable mid-run."""
    return config.resolve_faults(os.environ if env is None else env)


def mode_for(source: str, env=None) -> str | None:
    return active(env).get(source)


async def inject(source: str, stage: str) -> None:
    """Misbehave if a fault is configured for `source`; return immediately otherwise.

    fail    -> raise SourceFault at both stages.
    timeout -> a "startup" is allowed to succeed (the server is up, it is the calls that hang); a "call" sleeps
               config.FAULT_TIMEOUT_SLEEP_S, which is longer than the call timeout, so the REAL timeout must trip.
               The sleep is cancellable: it ends the moment the surrounding timeout cancels it.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; use one of {STAGES}")
    mode = mode_for(source)
    if mode == "fail":
        raise SourceFault(source, stage)
    if mode == "timeout" and stage == "call":
        await anyio.sleep(config.FAULT_TIMEOUT_SLEEP_S)


class FaultyClient:
    """Wrap an MCP-style client (async context manager with .tools and an awaitable .call) with fault injection.

    The wrapper enforces `call_timeout` itself so an injected hang ends as a normal timeout outcome, exactly as
    McpToolClient.call reports one. The outcome type is borrowed from the wrapped client's module.
    """

    def __init__(self, client: Any, source: str, call_timeout: float | None = None) -> None:
        self._client, self._source = client, source
        self._call_timeout = call_timeout if call_timeout is not None else config.MCP_CALL_TIMEOUT_S

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._client.tools

    async def __aenter__(self) -> "FaultyClient":
        await inject(self._source, "startup")  # raises SourceFault before the real server is launched
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.__aexit__(*exc_info)

    async def call(self, name: str, args: dict[str, Any]):
        from app.nodes.mcp_client import ToolOutcome  # local import: faults.py must stay importable on its own

        try:
            with anyio.fail_after(self._call_timeout):
                await inject(self._source, "call")
        except TimeoutError:
            return ToolOutcome(False, None, f"timeout: {name} took longer than {self._call_timeout}s (injected)", 0)
        except SourceFault as fault:
            return ToolOutcome(False, None, f"call_failed: {fault}", 0)
        return await self._client.call(name, args)
