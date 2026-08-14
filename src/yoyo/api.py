"""Local HTTP API.

Bound to 127.0.0.1 by default. This is a laptop; if you ever expose it, put it on the
tailnet with auth first — the whole point of the split is that the security boundary is
explicit, not accidental.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import core, doctor
from .config import get_settings
from .rag import retrieve as rag
from .storage import db

log = logging.getLogger(__name__)

app = FastAPI(title="Yoyo", version="0.1.0")


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    conversation_id: int | None = None
    role: str = "answer"
    use_rag: bool = True
    top_k: int | None = None


class PassageOut(BaseModel):
    chunk_id: int
    title: str | None
    source_path: str
    ordinal: int
    score: float
    text: str


class AskResponse(BaseModel):
    text: str
    model: str
    latency_ms: int
    conversation_id: int | None
    message_id: int | None
    passages: list[PassageOut]


@app.get("/health")
def health() -> dict[str, object]:
    return doctor.summary()


@app.get("/stats")
def stats() -> dict[str, int]:
    with db.connection() as conn:
        return db.stats(conn)


@app.post("/conversations")
def create_conversation(title: str | None = None) -> dict[str, int]:
    return {"conversation_id": core.new_conversation(title)}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    try:
        answer = core.ask(
            req.question,
            conversation_id=req.conversation_id,
            role=req.role,
            use_rag=req.use_rag,
            top_k=req.top_k,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AskResponse(
        text=answer.text,
        model=answer.model,
        latency_ms=answer.latency_ms,
        conversation_id=answer.conversation_id,
        message_id=answer.message_id,
        passages=[_passage_out(p) for p in answer.passages],
    )


@app.post("/ask/stream")
def ask_stream(req: AskRequest) -> StreamingResponse:
    """Streamed answer. Preferred over /ask: agent turns run 30-60 s, tool loops minutes.

    Reasoning traces are dropped — they are on by default and are not the answer.
    """
    from . import llm
    from .rag import retrieve as rag_mod

    passages = rag_mod.retrieve(req.question, top_k=req.top_k) if req.use_rag else []
    context = rag_mod.build_context(passages) if passages else ""
    messages: list[dict[str, object]] = [{"role": "system", "content": core.SYSTEM_PROMPT}]
    if req.conversation_id:
        messages.extend(core.history(req.conversation_id))
    messages.append(
        {"role": "user", "content": f"{context}\n\n---\n\n{req.question}" if context else req.question}
    )

    def gen():
        for piece in llm.stream_chat(messages, role=req.role):
            yield piece

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/search", response_model=list[PassageOut])
def search(q: str, top_k: int | None = None) -> list[PassageOut]:
    return [_passage_out(p) for p in rag.retrieve(q, top_k=top_k)]


def _passage_out(p: rag.Passage) -> PassageOut:
    return PassageOut(
        chunk_id=p.chunk_id,
        title=p.title,
        source_path=p.source_path,
        ordinal=p.ordinal,
        score=p.score,
        text=p.text,
    )


def serve() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(app, host=s.api_host, port=s.api_port, log_level=s.log_level.lower())
