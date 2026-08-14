"""Tool registry.

Tools are the precursor to MCP servers: same shape (name, description, JSON-schema
parameters, a callable), local for now. When the MCP servers land they register here too and
nothing above this layer changes.

Every tool must be **verifiable** — its result checkable without asking a model. That is what
makes the tool-fidelity eval possible: if a model skips the tool and invents an answer, the
difference has to be mechanically detectable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class ToolError(RuntimeError):
    pass


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def run(self, arguments: dict[str, Any]) -> Any:
        try:
            args = self.params(**(arguments or {}))
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"{self.name}: invalid arguments {arguments!r}: {exc}") from exc
        return self.fn(args)


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, params: type[BaseModel]):  # noqa: ANN201
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tools:
                raise ValueError(f"tool {name!r} already registered")
            self._tools[name] = Tool(name=name, description=description, params=params, fn=fn)
            return fn

        return deco

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolError(f"unknown tool {name!r}. Registered: {known}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = only if only is not None else self.names()
        return [self.get(n).spec() for n in names]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.get(name)
        log.info("tool call: %s(%s)", name, arguments)
        return tool.run(arguments)


registry = Registry()


# --------------------------------------------------------------- built-ins ---


class SearchArgs(BaseModel):
    query: str = Field(description="What to look for in the user's document corpus")
    top_k: int = Field(default=6, ge=1, le=20, description="How many passages to return")


@registry.register(
    "search_corpus",
    "Search the user's personal document corpus. Returns matching passages IN FULL with "
    "their chunk ids — you do not need to call read_chunk afterwards unless a passage "
    "clearly continues beyond what is returned.",
    SearchArgs,
)
def _search_corpus(args: SearchArgs) -> dict[str, Any]:
    from .rag import retrieve as rag

    passages = rag.retrieve(args.query, top_k=args.top_k)
    return {
        "count": len(passages),
        "passages": [
            {
                "chunk_id": p.chunk_id,
                "title": p.title,
                "source": p.source_path,
                "text": p.text,
            }
            for p in passages
        ],
    }


class ReadChunkArgs(BaseModel):
    chunk_id: int = Field(description="Chunk id, as returned by search_corpus")


@registry.register(
    "read_chunk",
    "Read one corpus chunk by id. Rarely needed — search_corpus already returns full "
    "passage text. Use only to fetch a neighbouring chunk you saw referenced.",
    ReadChunkArgs,
)
def _read_chunk(args: ReadChunkArgs) -> dict[str, Any]:
    from .storage import db

    with db.connection() as conn:
        rows = db.get_chunks(conn, [args.chunk_id])
    if not rows:
        raise ToolError(f"no chunk with id {args.chunk_id}")
    r = rows[0]
    return {
        "chunk_id": r["id"],
        "title": r["title"],
        "source": r["source_path"],
        "ordinal": r["ordinal"],
        "text": r["text"],
    }


class NoArgs(BaseModel):
    pass


@registry.register(
    "corpus_stats",
    "Exact counts of documents, chunks and embedded chunks in the user's corpus. "
    "These numbers cannot be guessed — call this rather than estimating.",
    NoArgs,
)
def _corpus_stats(_: NoArgs) -> dict[str, Any]:
    from .storage import db

    with db.connection() as conn:
        return db.stats(conn)


@registry.register(
    "current_time",
    "The current UTC date and time. You have no clock — call this rather than guessing.",
    NoArgs,
)
def _current_time(_: NoArgs) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {"utc": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")}
