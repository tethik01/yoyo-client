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
- Cite source ids exactly as given. Never construct file paths, URLs or links.
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


#: Auto-title length. Long enough to tell two conversations apart in a sidebar, short
#: enough not to wrap. Derived from the first question because asking the user to name a
#: conversation before having one is a step nobody takes.
TITLE_CHARS = 60


def title_for(question: str) -> str:
    text = " ".join((question or "").split())
    return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "") or "(untitled)"


def list_conversations(limit: int = 50) -> list[dict[str, object]]:
    """Most recently updated first — the order a sidebar wants."""
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS turns
                 FROM conversations c
                ORDER BY c.updated_at DESC, c.id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def conversation_messages(conversation_id: int, limit: int = 200) -> list[dict[str, object]]:
    """Oldest first — reading order, not history order."""
    with db.connection() as conn:
        rows = conn.execute(
            """SELECT id, role, content, model, latency_ms, created_at, metadata
                 FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        entry = dict(r)
        try:
            entry["metadata"] = json.loads(entry.get("metadata") or "{}")
        except json.JSONDecodeError:
            entry["metadata"] = {}
        out.append(entry)
    return out


def delete_conversation(conversation_id: int) -> None:
    with db.connection() as conn, db.transaction(conn):
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def persist_turn(
    conversation_id: int,
    question: str,
    answer: str,
    *,
    model: str = "",
    role: str = "",
    latency_ms: int = 0,
    metadata: dict | None = None,
) -> int:
    """Record one question/answer pair from ANY path — ask, agent or graph.

    `_persist` below is the RAG-specific version and also writes citations. This is the
    plain one, added because agent and graph turns were never persisted at all: the UI could
    show you a conversation and lose it on refresh, and a follow-up question had no idea what
    came before.
    """
    with db.connection() as conn, db.transaction(conn):
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?,'user',?)",
            (conversation_id, question),
        )
        cur = conn.execute(
            """INSERT INTO messages
                 (conversation_id, role, content, capability, model, latency_ms, metadata)
               VALUES (?,'assistant',?,?,?,?,?)""",
            (conversation_id, answer, role, model, latency_ms,
             json.dumps(metadata or {}, default=str)),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        # A conversation created before its first question has no title. Fill it in on the
        # first turn rather than leaving "(untitled)" rows in the sidebar forever.
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
            (title_for(question), conversation_id),
        )
        return int(cur.lastrowid)


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

    # `ask` has NO tools, so nothing here can legitimately produce a web link — the only
    # material available is the retrieved passages. Observed live in the UI: asked for local
    # news with no web tool mounted, the model answered with three plausible, clickable,
    # invented news-site URLs. The same provenance rule as the agent path, with retrieval
    # standing in for tool results.
    from . import citations

    sources = "\n".join(p.text for p in passages)
    answer_text, invented = citations.strip_unsupported_urls(result.text or "", sources)
    cleaned, paths = citations.strip_fabricated_links(answer_text)
    invented = sorted(set(invented) | set(paths))
    if invented:
        log.warning("stripped fabricated citation(s) from a RAG answer: %s", invented)

    message_id = None
    if conversation_id:
        message_id = _persist(
            conversation_id, question, result, passages, role, latency_ms
        )

    return Answer(
        text=cleaned,
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
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
            (title_for(question), conversation_id),
        )
    return message_id
