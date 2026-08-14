"""The turn loop: retrieve, prompt, answer, persist with citations."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from . import llm
from .rag import retrieve as rag
from .storage import db

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Yoyo, a personal assistant running on the user's own hardware.

Rules:
- Answer from the <source> blocks when they are relevant. Cite the source id inline like [12].
- If the sources do not contain the answer, say so plainly and answer from general knowledge,
  labelled as such. Do not blend the two silently.
- If a tool is available and relevant, call it. Never fabricate a result you could have
  looked up — an unavailable answer is better than an invented one.
- Be concise. No preamble."""


@dataclass(slots=True)
class Answer:
    text: str
    model: str
    passages: list[rag.Passage]
    latency_ms: int
    endpoint: str = ""
    reasoning: str | None = None
    conversation_id: int | None = None
    message_id: int | None = None


def new_conversation(title: str | None = None) -> int:
    with db.connection() as conn, db.transaction(conn):
        cur = conn.execute("INSERT INTO conversations (title) VALUES (?)", (title,))
        return int(cur.lastrowid)


def history(conversation_id: int, limit: int = 20) -> list[dict[str, str]]:
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT role, content FROM messages
                WHERE conversation_id = ? ORDER BY id DESC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def ask(
    question: str,
    *,
    conversation_id: int | None = None,
    role: str = "answer",
    use_rag: bool = True,
    top_k: int | None = None,
) -> Answer:
    started = time.monotonic()

    passages = rag.retrieve(question, top_k=top_k) if use_rag else []
    context = rag.build_context(passages) if passages else ""

    messages: list[dict[str, object]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if conversation_id:
        messages.extend(history(conversation_id))
    user_content = f"{context}\n\n---\n\n{question}" if context else question
    messages.append({"role": "user", "content": user_content})

    result = llm.chat(messages, role=role)
    latency_ms = int((time.monotonic() - started) * 1000)

    message_id = None
    if conversation_id:
        message_id = _persist(
            conversation_id, question, result, passages, role, latency_ms
        )

    return Answer(
        text=result.text,
        model=result.model,
        passages=passages,
        latency_ms=latency_ms,
        endpoint=result.endpoint,
        reasoning=result.reasoning,
        conversation_id=conversation_id,
        message_id=message_id,
    )


def _persist(
    conversation_id: int,
    question: str,
    result: llm.ChatResult,
    passages: list[rag.Passage],
    role: str,
    latency_ms: int,
) -> int:
    with db.connection() as conn, db.transaction(conn):
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?,'user',?)",
            (conversation_id, question),
        )
        cur = conn.execute(
            """INSERT INTO messages
                 (conversation_id, role, content, capability, model,
                  prompt_tokens, completion_tokens, latency_ms, metadata)
               VALUES (?,'assistant',?,?,?,?,?,?,?)""",
            (
                conversation_id,
                result.text,
                role,
                result.model,
                result.prompt_tokens,
                result.completion_tokens,
                latency_ms,
                json.dumps({"passages": len(passages), "endpoint": result.endpoint,
                            "reasoning_chars": len(result.reasoning or "")}),
            ),
        )
        message_id = int(cur.lastrowid)
        if passages:
            conn.executemany(
                "INSERT OR REPLACE INTO message_citations (message_id, chunk_id, rank, score) "
                "VALUES (?,?,?,?)",
                [(message_id, p.chunk_id, i, p.score) for i, p in enumerate(passages)],
            )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
    return message_id
