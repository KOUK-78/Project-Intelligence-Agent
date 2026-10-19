"""MCP client wrapper: launches the Jira server as a stdio subprocess on demand. OWNER: B.

Launch: by default runs `python -m app.mcp_servers.jira_server` from the repo root (so the
server imports app.config and shares TODAY / DATA_PATH). Pass `server_path` to run a script
instead (used by the failure tests).

Failure policy (matters for the later tool-failure evals):
  * startup failure (server won't launch / initialise / list tools) -> McpStartupError,
    and the agent stops cleanly with status="error".
  * a failed call (timeout, crashed server, tool error, malformed response) -> `call()`
    NEVER raises; it returns ToolOutcome(ok=False, error=...) so the graph can record it
    and decide whether to continue.
The server must never print to stdout; its stderr is passed through to ours.
"""
import json
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import config
from app.nodes import settings


class McpStartupError(RuntimeError):
    """The MCP server could not be started or initialised."""


@dataclass
class ToolOutcome:
    ok: bool
    data: Any = None
    error: str | None = None
    latency_ms: int = 0


def _describe(exc: BaseException) -> str:
    """One-line description; unwraps anyio ExceptionGroups."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _describe(exc.exceptions[0])
    return f"{type(exc).__name__}: {exc}"[:300]


def _text_of(result: Any) -> str:
    parts = [c.text for c in (getattr(result, "content", None) or []) if getattr(c, "type", None) == "text"]
    return "\n".join(parts)


def parse_tool_result(result: Any) -> tuple[bool, Any, str | None]:
    """CallToolResult -> (ok, data, error). Works with mcp 1.x and 2.x attribute names."""
    text = _text_of(result)
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        return False, None, f"tool_error: {text[:300] or 'unknown'}"

    data: Any = None
    if text.strip():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if data is None:
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # Some SDK versions wrap a dict return value as {"result": {...}}.
            data = structured["result"] if list(structured) == ["result"] else structured
    if not isinstance(data, dict):
        return False, None, f"malformed_response: expected a JSON object, got {text[:120]!r}"
    return True, data, None


class McpToolClient:
    """Async context manager. Enter and exit it in the same task (anyio requirement)."""

    def __init__(
        self,
        server_path: str | Path | None = None,
        *,
        env: dict[str, str] | None = None,
        call_timeout: float | None = None,
        startup_timeout: float | None = None,
    ) -> None:
        self._server_path = str(server_path) if server_path else None
        self._env = env
        self._call_timeout = call_timeout if call_timeout is not None else settings.get("MCP_CALL_TIMEOUT_S")
        self._startup_timeout = startup_timeout if startup_timeout is not None else settings.get("MCP_STARTUP_TIMEOUT_S")
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []

    @property
    def tools(self) -> list[dict[str, Any]]:
        """[{"name", "description", "input_schema"}] as advertised by the server."""
        return self._tools

    async def __aenter__(self) -> "McpToolClient":
        stack = AsyncExitStack()
        try:
            if self._server_path:
                params = StdioServerParameters(command=sys.executable, args=[self._server_path], env=self._env)
            else:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "app.mcp_servers.jira_server"],
                    cwd=config.ROOT,
                    env=self._env,
                )
            # The subprocess spawn itself is outside any timeout scope on purpose: anyio
            # cancel scopes must nest properly with the transport's task group.
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            with anyio.fail_after(self._startup_timeout):
                await session.initialize()
                listed = await session.list_tools()
            self._tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {},
                }
                for t in listed.tools
            ]
            if not self._tools:
                raise McpStartupError("server advertised no tools")
        except McpStartupError:
            await self._close(stack)
            raise
        except Exception as exc:
            await self._close(stack)
            raise McpStartupError(f"could not start MCP server {self._server_path or 'jira_server'}: {_describe(exc)}") from exc
        self._stack, self._session = stack, session
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._close(self._stack)
        self._stack = self._session = None

    @staticmethod
    async def _close(stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 - teardown must never mask the real error
            pass

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Call one tool. Never raises (except task cancellation)."""
        start = time.perf_counter()

        def done(ok: bool, data: Any = None, error: str | None = None) -> ToolOutcome:
            return ToolOutcome(ok, data, error, int((time.perf_counter() - start) * 1000))

        if self._session is None:
            return done(False, error="not_connected: MCP session is not open")
        try:
            with anyio.fail_after(self._call_timeout):
                result = await self._session.call_tool(name, args)
        except TimeoutError:
            return done(False, error=f"timeout: {name} took longer than {self._call_timeout}s")
        except Exception as exc:  # noqa: BLE001 - transport closed, protocol error, ...
            return done(False, error=f"call_failed: {_describe(exc)}")
        ok, data, error = parse_tool_result(result)
        return done(ok, data, error)
