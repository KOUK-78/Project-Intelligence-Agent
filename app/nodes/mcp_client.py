"""MCP client wrapper and multi-server router (stdio subprocesses). OWNER: D.

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
import asyncio
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

from app import config, faults


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
    return f"{type(exc).__name__}: {exc}"[: config.ERROR_TEXT_CHARS]


def _text_of(result: Any) -> str:
    parts = [c.text for c in (getattr(result, "content", None) or []) if getattr(c, "type", None) == "text"]
    return "\n".join(parts)


def parse_tool_result(result: Any) -> tuple[bool, Any, str | None]:
    """CallToolResult -> (ok, data, error). Works with mcp 1.x and 2.x attribute names."""
    text = _text_of(result)
    flag = result.is_error if hasattr(result, "is_error") else getattr(result, "isError", False)  # 1.x name
    if flag:
        return False, None, f"tool_error: {text[: config.ERROR_TEXT_CHARS] or 'unknown'}"

    data: Any = None
    if text.strip():
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if data is None:
        structured = result.structured_content if hasattr(result, "structured_content") else getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # Some SDK versions wrap a dict return value as {"result": {...}}.
            data = structured["result"] if list(structured) == ["result"] else structured
    if not isinstance(data, dict):
        return False, None, f"malformed_response: expected a JSON object, got {text[: config.SNIPPET_CHARS]!r}"
    return True, data, None


class McpToolClient:
    """Async context manager. Enter and exit it in the same task (anyio requirement)."""

    def __init__(
        self,
        server_path: str | Path | None = None,
        *,
        server_module: str = "app.mcp_servers.jira_server",
        env: dict[str, str] | None = None,
        call_timeout: float | None = None,
        startup_timeout: float | None = None,
        source: str | None = None,
    ) -> None:
        self._source = source  # registry name; lets FAULT_INJECT target this server (app/faults.py)
        self._server_path = str(server_path) if server_path else None
        self._server_module = server_module
        self._env = env
        self._call_timeout = call_timeout if call_timeout is not None else config.MCP_CALL_TIMEOUT_S
        self._startup_timeout = startup_timeout if startup_timeout is not None else config.MCP_STARTUP_TIMEOUT_S
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
            if self._source:
                await faults.inject(self._source, "startup")  # injected "fail": raise before any server is launched
            if self._server_path:
                params = StdioServerParameters(command=sys.executable, args=[self._server_path], env=self._env)
            else:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", self._server_module],
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
            raise McpStartupError(f"could not start MCP server {self._server_path or self._server_module}: {_describe(exc)}") from exc
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
                if self._source:
                    await faults.inject(self._source, "call")  # inside the timeout: an injected hang trips the real one
                result = await self._session.call_tool(name, args)
        except TimeoutError:
            return done(False, error=f"timeout: {name} took longer than {self._call_timeout}s")
        except Exception as exc:  # noqa: BLE001 - transport closed, protocol error, ...
            return done(False, error=f"call_failed: {_describe(exc)}")
        ok, data, error = parse_tool_result(result)
        return done(ok, data, error)


class McpRouter:
    """Several stdio servers behind the McpToolClient interface (tools, call, async context manager).

    * One McpToolClient per registered source. Each lives in its OWN task (anyio needs enter and
      exit in the same task), so servers can start concurrently.
    * `lazy=False` (default): every source starts at enter, all at once. `lazy=True`: nothing starts
      until `await ensure([...])`, so a question that needs one source pays for one launch.
    * A source that fails to start is recorded in `unavailable` and the run continues. Only when NO
      source starts does enter raise McpStartupError (Phase 1 behaviour for a Jira-only setup).
    * Two servers advertising the same tool name is a startup error: routing would be ambiguous.
    * `call()` never raises; a tool of an unavailable source returns ok=False, "source_unavailable".
    """

    def __init__(
        self,
        sources: list | None = None,
        *,
        env: dict[str, dict[str, str]] | None = None,
        clients: dict[str, Any] | None = None,
        lazy: bool = False,
        **client_kwargs: Any,
    ) -> None:
        from app import sources as registry

        self._sources = list(sources) if sources is not None else registry.all_sources()
        self._env = env or {}
        self._injected = clients or {}
        self._client_kwargs = client_kwargs
        self._lazy = lazy
        self._clients: dict[str, Any] = {}
        self._runners: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self._route: dict[str, str] = {}
        self.unavailable: dict[str, str] = {}

    # ---- interface shared with McpToolClient
    @property
    def tools(self) -> list[dict[str, Any]]:
        return [t for name in self._clients for t in self._clients[name].tools]

    def tools_for(self, source: str) -> list[dict[str, Any]]:
        return list(self._clients[source].tools) if source in self._clients else []

    @property
    def started(self) -> list[str]:
        return list(self._clients)

    async def __aenter__(self) -> "McpRouter":
        if not self._lazy:
            await self.ensure([s.name for s in self._sources])
            if not self._clients:
                raise McpStartupError("no MCP server could be started: " + "; ".join(
                    f"{n}: {e}" for n, e in self.unavailable.items()))
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for _, stop in self._runners.values():
            stop.set()
        await asyncio.gather(*(task for task, _ in self._runners.values()), return_exceptions=True)
        self._runners.clear()
        self._clients.clear()

    # ---- starting
    def _make_client(self, source: Any) -> Any:
        if source.name in self._injected:
            return self._injected[source.name]
        return McpToolClient(server_module=source.server_module, env=self._env.get(source.name), source=source.name,
                             **self._client_kwargs)

    async def _run(self, name: str, client: Any, ready: "asyncio.Future[str | None]", stop: asyncio.Event) -> None:
        try:
            async with client:
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except Exception as exc:  # noqa: BLE001 - startup failure becomes "source unavailable"
            if not ready.done():
                ready.set_result(_describe(exc))

    async def ensure(self, names: list[str]) -> dict[str, str]:
        """Start the named sources that are not running yet, concurrently. Returns {source: error}
        for those that failed (also kept in `unavailable`)."""
        by_name = {s.name: s for s in self._sources}
        todo = [n for n in dict.fromkeys(names) if n in by_name and n not in self._clients and n not in self.unavailable]
        loop = asyncio.get_running_loop()
        pending: dict[str, asyncio.Future] = {}
        for n in todo:
            client, ready, stop = self._make_client(by_name[n]), loop.create_future(), asyncio.Event()
            task = asyncio.create_task(self._run(n, client, ready, stop))
            self._runners[n] = (task, stop)
            self._clients[n] = client
            pending[n] = ready
        failed: dict[str, str] = {}
        for n, err in zip(pending, await asyncio.gather(*pending.values())):
            if err is not None:
                failed[n] = err
                self._drop(n, err)
        self._index_tools()
        return failed

    def _drop(self, name: str, error: str) -> None:
        self.unavailable[name] = error
        self._clients.pop(name, None)
        runner = self._runners.pop(name, None)
        if runner:
            runner[1].set()

    def _index_tools(self) -> None:
        route: dict[str, str] = {}
        for name, client in list(self._clients.items()):
            for tool in client.tools:
                owner = route.get(tool["name"])
                if owner is not None and owner != name:
                    raise McpStartupError(f"duplicate tool name {tool['name']!r} from sources {owner!r} and {name!r}")
                route[tool["name"]] = name
        self._route = route

    # ---- calling
    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        source = self._route.get(name)
        if source is None:
            owner = next((s for s in self._sources if name in s.tool_names), None)
            if owner is not None and owner.name in self.unavailable:
                return ToolOutcome(False, error=f"source_unavailable: {owner.name}: {self.unavailable[owner.name]}")
            return ToolOutcome(False, error=f"unknown_tool: {name!r}")
        return await self._clients[source].call(name, args)
