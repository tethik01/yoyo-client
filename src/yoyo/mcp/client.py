"""MCP client adapter — mounts MCP servers into Yoyo's tool registry.

One adapter, and every MCP server becomes available to the agent loop without a line
changing above `tools.Registry`. Off-the-shelf servers (filesystem, git, fetch) and
first-party ones (yoyo-vault) arrive through the same door.

**Threading.** The MCP SDK is async; Yoyo's agent loop is synchronous. Rather than colour
the whole codebase async for one integration, each session owns a daemon thread running its
own event loop, and calls cross into it with `run_coroutine_threadsafe`. The connection is
persistent — spawning a subprocess per tool call would cost more than the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, create_model

from ..config import REPO_ROOT
from ..tools import Tool, ToolError, registry as default_registry

log = logging.getLogger(__name__)

CONFIG_FILE = REPO_ROOT / "yoyo-mcp.yaml"
CONNECT_TIMEOUT_S = 30
CALL_TIMEOUT_S = 120


class MCPError(RuntimeError):
    pass


def _describe(exc: BaseException) -> str:
    """Flatten ExceptionGroups to the causes that actually explain the failure.

    anyio task groups raise `ExceptionGroup`, whose str() is "unhandled errors in a
    TaskGroup (1 sub-exception)" — true, and completely useless to whoever has to fix it.
    """
    group = getattr(exc, "exceptions", None)
    if group:
        return "; ".join(_describe(e) for e in group)
    name = type(exc).__name__
    text = str(exc).strip()
    if isinstance(exc, FileNotFoundError):
        return f"command not found: {exc.filename or text}"
    return f"{name}: {text}" if text else name


@dataclass
class ServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    prefix: str | None = None   # tool-name prefix; defaults to the server name

    def tool_name(self, remote: str) -> str:
        """Namespace a remote tool, without stuttering.

        Servers often namespace their own tools already (`yoyo-vault` exposes
        `vault_search`), and blindly prefixing produced `vault_vault_search`. Skip the
        prefix when the remote name already carries it.
        """
        p = self.prefix if self.prefix is not None else self.name
        if not p:
            return remote
        if remote == p or remote.startswith(f"{p}_"):
            return remote
        return f"{p}_{remote}"

    def resolved_command(self) -> str:
        """A bare `python` is ambiguous — on Windows it may resolve outside the venv, or
        to nothing. For our own servers, run the interpreter that is running Yoyo."""
        import sys

        if self.command in {"python", "python3", "py"}:
            return sys.executable
        return self.command


class Session:
    """A live connection to one MCP server, driven from a background event loop."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stop = threading.Event()
        # The child's stderr is where servers actually explain themselves. anyio wraps the
        # resulting failure in an ExceptionGroup, so without this the user sees
        # "unhandled errors in a TaskGroup" and nothing else.
        #
        # It must be a REAL file, not StringIO: the child process is given this handle
        # directly, so it needs a fileno(). StringIO silently works on Linux and fails on
        # Windows with "UnsupportedOperation: fileno".
        self._errlog = tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        )
        self.tools: list[Any] = []

    # ---------------------------------------------------------- lifecycle ---

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"mcp-{self.spec.name}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(CONNECT_TIMEOUT_S):
            message = (
                f"{self.spec.name}: timed out connecting after {CONNECT_TIMEOUT_S}s"
                f"{self._stderr_hint()}"
            )
            self.close()
            raise MCPError(message)
        if self._error:
            message = f"{self.spec.name}: {_describe(self._error)}{self._stderr_hint()}"
            self.close()
            raise MCPError(message)

    def _stderr_hint(self) -> str:
        try:
            self._errlog.flush()
            self._errlog.seek(0)
            captured = self._errlog.read().strip()
        except (OSError, ValueError):
            return ""
        if not captured:
            return ""
        tail = "\n".join(captured.splitlines()[-5:])
        return f"\n  server said: {tail}"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller via _error
            self._error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.spec.resolved_command(),
            args=self.spec.args,
            env={**os.environ, **self.spec.env},
            cwd=self.spec.cwd,
        )
        async with stdio_client(params, errlog=self._errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self.tools = (await session.list_tools()).tools
                log.info(
                    "mounted MCP server %s: %s",
                    self.spec.name,
                    ", ".join(t.name for t in self.tools) or "<no tools>",
                )
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.1)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self._errlog.close()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------- calling ---

    def call(self, remote_name: str, arguments: dict[str, Any]) -> Any:
        if self._loop is None or self._session is None:
            raise MCPError(f"{self.spec.name}: not connected")

        fut: Future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(remote_name, arguments or {}), self._loop
        )
        try:
            result = fut.result(timeout=CALL_TIMEOUT_S)
        except TimeoutError as exc:
            raise ToolError(
                f"{self.spec.name}.{remote_name} timed out after {CALL_TIMEOUT_S}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"{self.spec.name}.{remote_name} failed: {exc}") from exc

        return _unwrap(result, f"{self.spec.name}.{remote_name}")


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first attribute that exists.

    The SDK renamed its fields between 1.x (camelCase: `inputSchema`, `isError`,
    `structuredContent`) and 2.x (snake_case). Reading both keeps one adapter working
    across versions instead of pinning the project to one SDK generation.
    """
    for n in names:
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return default


def _unwrap(result: Any, label: str) -> Any:
    """Turn an MCP CallToolResult into a plain Python value.

    Servers may return structured content, JSON in a text block, or prose. Prefer the most
    structured form available and fall back gracefully rather than guessing.
    """
    if _attr(result, "is_error", "isError", default=False):
        raise ToolError(f"{label}: {_text_of(result) or 'server reported an error'}")

    structured = _attr(result, "structured_content", "structuredContent")
    if structured:
        # FastMCP wraps bare returns as {"result": ...}; unwrap that for readability.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured

    text = _text_of(result)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _text_of(result: Any) -> str | None:
    blocks = [c for c in getattr(result, "content", []) or [] if getattr(c, "type", "") == "text"]
    if not blocks:
        return None
    return "\n".join(b.text for b in blocks)


# ----------------------------------------------------------------- adapting ---

_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _params_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Build a pydantic model from an MCP tool's JSON schema.

    Only the top level is translated. Nested structure is passed through as dict/list —
    enough for validation and for regenerating a schema the model can read, without
    reimplementing JSON Schema.
    """
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    fields: dict[str, Any] = {}

    for field_name, spec in properties.items():
        py = _JSON_TO_PY.get((spec or {}).get("type"), Any)
        default = ... if field_name in required else (spec or {}).get("default", None)
        fields[field_name] = (py if field_name in required else (py | None if py is not Any else Any), default)

    if not fields:
        return create_model(f"{name}_Args")
    return create_model(f"{name}_Args", **fields)


def adapt(session: Session, remote_tool: Any) -> Tool:
    local_name = session.spec.tool_name(remote_tool.name)
    schema = _attr(remote_tool, "input_schema", "inputSchema", default={}) or {}
    params = _params_model(local_name, schema)
    description = (remote_tool.description or f"{remote_tool.name} (via {session.spec.name})").strip()

    def fn(args: BaseModel, _s=session, _r=remote_tool.name):  # noqa: ANN001
        return _s.call(_r, args.model_dump(exclude_none=True))

    return Tool(name=local_name, description=description, params=params, fn=fn)


# ------------------------------------------------------------------ mounting ---

_sessions: dict[str, Session] = {}


def load_config(path: Path | None = None) -> list[ServerSpec]:
    path = path or CONFIG_FILE
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        ServerSpec(name=name, **(body or {}))
        for name, body in (raw.get("servers") or {}).items()
    ]


def mount(spec: ServerSpec, into=None) -> list[str]:  # noqa: ANN001
    into = into if into is not None else default_registry

    import shutil

    command = spec.resolved_command()
    if not shutil.which(command) and not Path(command).exists():
        raise MCPError(
            f"{spec.name}: command {command!r} not found on PATH. "
            f"For a Python server, use the interpreter running Yoyo "
            f"(sys.executable) rather than a bare 'python'."
        )

    session = Session(spec)
    session.start()
    _sessions[spec.name] = session

    names: list[str] = []
    for remote in session.tools:
        tool = adapt(session, remote)
        into.add(tool)
        names.append(tool.name)
    return names


def mount_all(path: Path | None = None, into=None) -> dict[str, Any]:  # noqa: ANN001
    """Mount every enabled server from the config. One failure does not stop the rest."""
    report: dict[str, Any] = {}
    for spec in load_config(path):
        if not spec.enabled:
            report[spec.name] = {"ok": True, "skipped": "disabled"}
            continue
        try:
            report[spec.name] = {"ok": True, "tools": mount(spec, into=into)}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not mount %s: %s", spec.name, exc)
            report[spec.name] = {"ok": False, "error": str(exc)}
    return report


def unmount_all() -> None:
    for session in list(_sessions.values()):
        session.close()
    _sessions.clear()
