"""Local HTTP API.

Bound to 127.0.0.1 by default. This is a laptop; if you ever expose it, put it on the
tailnet with auth first — the whole point of the split is that the security boundary is
explicit, not accidental.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import auth, core, doctor, jobs, router
from .config import get_settings
from .mcp import client as mcp_client
from .rag import retrieve as rag
from .storage import db

log = logging.getLogger(__name__)

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN202
    """Mount MCP servers once, at startup.

    The CLI calls `mount_all()` before every agent turn. The API did not, so tools reached
    the model through the UI as the four built-ins only — and an agent asked to search the
    web replied "unknown tool 'web_search'". The tools existed, were configured and were
    enabled; nothing had told this process about them.

    At startup rather than per request: each mount spawns a subprocess over stdio, and doing
    that on every question would add seconds to each turn and leak processes.
    """
    # Apply pending migrations before serving a single request.
    #
    # Observed live: the owner opened the UI, clicked "Run doctor", and got a 500 with
    # `no such table: jobs` in the console and NOTHING on screen. He had not run
    # `yoyo migrate` — and there is no good reason he should have had to. This is a
    # single-user local app; a schema the code needs is not a decision to delegate, and
    # "remember to run a command after every update" is a trap that only ever fires when
    # you are trying to do something else.
    try:
        applied = db.migrate()
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
    except Exception:  # noqa: BLE001 - a broken schema must not stop the health endpoint
        log.exception("could not apply migrations")

    report = mcp_client.mount_all()
    for name, outcome in report.items():
        if outcome["ok"]:
            log.info("mounted MCP server %s", name)
        else:
            log.warning("MCP server %s failed to mount: %s", name, outcome["error"])
    app.state.mcp = report
    try:
        yield
    finally:
        mcp_client.unmount_all()


app = FastAPI(title="Yoyo", version="0.1.0", lifespan=lifespan)


#: Routes that change something, and are therefore worth stealing. Everything else stays
#: open on loopback: gating reads would add friction with no attacker to stop, since a page
#: that cannot read a cross-origin response learns nothing by triggering one.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def guard(request: Request, call_next):  # noqa: ANN001, ANN201
    """Token + Origin on every state-changing request.

    See `auth.py` for why loopback is not a boundary. Short version: any page you visit can
    POST to 127.0.0.1, and the moment a POST ingests files or writes memory that matters.
    A custom header cannot be set cross-origin without a preflight this server never
    answers, so the token is what actually closes it; the Origin check is the second lock.
    """
    if request.method in SAFE_METHODS:
        return await call_next(request)

    settings = get_settings()
    origin = request.headers.get("origin")
    if not auth.origin_is_allowed(origin, settings.api_host, settings.api_port):
        log.warning("rejected %s %s from origin %s", request.method, request.url.path, origin)
        return JSONResponse({"detail": f"origin {origin!r} is not allowed"}, status_code=403)

    supplied = request.headers.get(auth.TOKEN_HEADER, "")
    if not auth.constant_time_equal(supplied, auth.read_or_create_token()):
        return JSONResponse(
            {"detail": f"missing or wrong {auth.TOKEN_HEADER} header. The UI gets this "
                       f"automatically; a script can read data/ui-token."},
            status_code=401,
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    """Every failure reaches the browser as JSON with a message.

    Before this, an unexpected error was a 500 with an HTML body: a full traceback in the
    server console and a blank panel in the UI. The person looking at the screen learned
    nothing, which is the same complaint as an answer that cites nothing — if the system
    knows why it failed, it has to say so where the human is looking.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"detail": f"{type(exc).__name__}: {exc}"}, status_code=500
    )


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
    from . import tools as tools_mod

    summary = doctor.summary()
    # Which tools the model can actually reach. This is the fact whose absence produced
    # "unknown tool 'web_search'" against a correctly configured, enabled server.
    summary["tools"] = sorted(tools_mod.registry.names())
    summary["mcp"] = {
        name: outcome["ok"] for name, outcome in getattr(app.state, "mcp", {}).items()
    }
    return summary


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
        {
            "role": "user",
            "content": f"{context}\n\n---\n\n{req.question}" if context else req.question,
        }
    )

    def gen():
        yield from llm.stream_chat(messages, role=req.role)

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


# --------------------------------------------------------------------- agent ---


class AgentRequest(BaseModel):
    question: str = Field(min_length=1)
    mode: str = "auto"           # auto | ask | agent | plan
    role: str | None = None
    max_iterations: int = 8
    conversation_id: int | None = None


@app.get("/conversations")
def list_conversations(limit: int = 50) -> list[dict[str, object]]:
    return core.list_conversations(limit=limit)


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int) -> dict[str, object]:
    messages = core.conversation_messages(conversation_id)
    if not messages:
        # An empty conversation and a missing one are different, but only the second is
        # worth a 404 — a conversation created and abandoned should not read as an error.
        known = {c["id"] for c in core.list_conversations(limit=500)}
        if conversation_id not in known:
            raise HTTPException(404, f"no conversation {conversation_id}")
    return {"id": conversation_id, "messages": messages}


@app.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: int) -> dict[str, object]:
    core.delete_conversation(conversation_id)
    return {"deleted": conversation_id}


class RouteRequest(BaseModel):
    question: str = Field(min_length=1)
    rules_only: bool = False


@app.post("/route")
def route_question(req: RouteRequest) -> dict[str, object]:
    """How a question would be routed, without answering it.

    Separate from `/agent/stream` on purpose: a misrouted answer and a bad answer look
    identical from the outside, and this is the only way to tell them apart.
    """
    try:
        return router.route(req.question, use_model=not req.rules_only).as_dict()
    except router.RouteError as exc:
        raise HTTPException(422, str(exc)) from exc


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/agent/stream")
def agent_stream(req: AgentRequest) -> StreamingResponse:
    """An agent turn as server-sent events: tool calls as they happen, then the answer.

    The CLI already shows tool calls; nothing else could. That visibility is most of the
    reason to have a UI at all — an answer you watch being assembled from named sources is
    a different object from an answer that simply appears.

    The agent loop is synchronous and returns everything at the end, so this runs it on a
    thread and emits events from a queue. Not true token streaming for the agent path —
    `/ask/stream` covers that — but the tool calls arrive live, which is the part with no
    other way to see it.
    """
    import queue
    import threading

    from . import agent as agent_mod

    if req.mode not in {"auto", "ask", "agent", "plan"}:
        raise HTTPException(422, "mode must be auto, ask, agent or plan")

    events: queue.Queue = queue.Queue()

    # Created lazily on the first question so an opened-and-abandoned tab leaves no rows.
    conversation_id = req.conversation_id or core.new_conversation()
    prior = core.history(conversation_id) if req.conversation_id else []

    def work() -> None:
        try:
            # `auto` routes here rather than before the thread starts: classification is a
            # model call, and blocking the SSE handshake on it makes the UI look hung on
            # every question. The choice is emitted as its own event the moment it is made,
            # so the browser can show — and offer to override — what was picked.
            mode, question = req.mode, req.question
            if mode == "auto":
                decision = router.route(req.question)
                mode, question = decision.mode, decision.question
                events.put(("route", decision.as_dict()))

            if mode == "ask":
                answer = core.ask(
                    question, role=req.role or "answer",
                    conversation_id=conversation_id,
                )
                for p in answer.passages:
                    events.put(("source", {"citation": f"[{p.chunk_id}]", "title": p.title,
                                           "path": p.source_path}))
                events.put(("answer", {"text": answer.text, "model": answer.model,
                                       "latency_ms": answer.latency_ms}))
            elif mode == "agent":
                result = agent_mod.run(
                    question,
                    role=req.role or "supervisor",
                    max_iterations=req.max_iterations,
                    history=prior,
                )
                core.persist_turn(
                    conversation_id, question, result.text,
                    model=result.model, role=req.role or "supervisor",
                    latency_ms=result.latency_ms,
                    metadata={"mode": "agent", "tools": result.tools_called,
                              "fabricated_links": result.fabricated_links},
                )
                for inv in result.invocations:
                    events.put(("tool", {"name": inv.name, "arguments": inv.arguments,
                                         "ok": inv.ok, "error": inv.error}))
                events.put(("answer", {
                    "text": result.text, "model": result.model,
                    "latency_ms": result.latency_ms, "iterations": result.iterations,
                    "stopped_because": result.stopped_because,
                    # Surfaced, never swallowed: a turn that needed the scrubber is a turn
                    # whose model fabricated a citation.
                    "fabricated_links": result.fabricated_links,
                }))
            else:
                from .graph import run as graph_run

                result = graph_run(question)
                core.persist_turn(
                    conversation_id, question, result.answer,
                    model="graph", latency_ms=result.latency_ms,
                    metadata={"mode": "plan", "subtasks": result.subtask_count},
                )
                if result.plan:
                    for st in result.plan.subtasks:
                        events.put(("subtask", {"id": st.id, "goal": st.goal}))
                for r in result.results:
                    events.put(("tool", {"name": f"subtask {r.subtask_id}", "arguments": {},
                                         "ok": r.ok, "error": None}))
                events.put(("answer", {"text": result.answer, "model": "graph",
                                       "latency_ms": result.latency_ms,
                                       "iterations": result.subtask_count,
                                       "stopped_because": result.stopped_because,
                                       "fabricated_links": []}))
        except Exception as exc:  # noqa: BLE001 - the browser must hear about it
            log.exception("agent stream failed")
            events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put((None, None))

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield _sse("start", {"mode": req.mode, "question": req.question,
                             "conversation_id": conversation_id})
        while True:
            kind, payload = events.get()
            if kind is None:
                break
            yield _sse(kind, payload)
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/citation/{citation}")
def resolve_citation(citation: str) -> dict[str, object]:
    """Resolve a citation to its source. The whole point of citing.

    Three vocabularies, one endpoint: `12` is a corpus chunk, `mail:<id>` is a message,
    anything else is treated as a vault note path. A citation you cannot follow is
    decoration, and until now following one meant a second terminal.
    """
    if citation.startswith("mail:"):
        from . import mail as mail_mod

        try:
            message = mail_mod.resolve(None).read(citation.removeprefix("mail:"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(404, f"could not read {citation}: {exc}") from exc
        return {"kind": "mail", **message.as_dict(include_body=True)}

    if citation.strip("[]").isdigit():
        from .storage import db as db_mod

        with db_mod.connection() as conn:
            row = conn.execute(
                "SELECT c.id, c.text, c.ordinal, d.title, d.source_path "
                "FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
                (int(citation.strip("[]")),),
            ).fetchone()
        if not row:
            raise HTTPException(404, f"no chunk {citation}")
        return {"kind": "chunk", "chunk_id": row[0], "text": row[1], "ordinal": row[2],
                "title": row[3], "source_path": row[4]}

    from . import vault as vault_mod

    try:
        return {"kind": "note", **vault_mod.read_note(citation.strip("[]"))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"could not resolve {citation}: {exc}") from exc


@app.get("/vault/graph")
def vault_graph(folder: str = "") -> dict[str, object]:
    """Notes, their wikilinks, and which of them the corpus has ingested."""
    from . import vault as vault_mod

    try:
        return vault_mod.graph(folder)
    except vault_mod.VaultError as exc:
        raise HTTPException(404, str(exc)) from exc


class JobRequest(BaseModel):
    kind: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)


@app.get("/jobs")
def list_jobs(limit: int = 50, kind: str | None = None) -> dict[str, object]:
    return {"kinds": jobs.kinds(), "jobs": jobs.recent(limit=limit, kind=kind)}


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, object]:
    row = jobs.get(job_id)
    if row is None:
        raise HTTPException(404, f"no job {job_id}")
    return row


@app.post("/jobs")
def start_job(req: JobRequest) -> dict[str, object]:
    try:
        job_id = jobs.create(req.kind, req.args)
    except jobs.JobError as exc:
        raise HTTPException(422, str(exc)) from exc
    jobs.start(job_id)
    return {"job_id": job_id, "kind": req.kind}


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict[str, object]:
    if jobs.get(job_id) is None:
        raise HTTPException(404, f"no job {job_id}")
    # False means "not running" — including finished a moment ago. Not an error.
    return {"job_id": job_id, "cancelling": jobs.cancel(job_id)}


@app.get("/jobs/{job_id}/stream")
def stream_job(job_id: int) -> StreamingResponse:
    """Replayed history, then live events. A reload during a five-minute eval must show the
    lines that already happened, or it looks like nothing is running."""
    if jobs.get(job_id) is None:
        raise HTTPException(404, f"no job {job_id}")

    def gen():  # noqa: ANN202
        for event, payload in jobs.subscribe(job_id):
            yield _sse(event, payload if isinstance(payload, dict) else {"line": payload})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------- memory ---
# Proposals in, decisions out, and exactly one route that writes a page.


class DecisionRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    note: str | None = None


class SubjectDecision(BaseModel):
    subject: str = Field(min_length=1)
    status: str = Field(pattern="^(approved|rejected)$")


@app.get("/memory/proposals")
def memory_proposals(limit: int = 200) -> dict[str, object]:
    from .memory import review

    return {"pending": review.pending(limit=limit), "stats": review.stats()}


@app.post("/memory/proposals/{proposal_id}")
def decide_proposal(proposal_id: int, req: DecisionRequest) -> dict[str, object]:
    from .memory import review

    changed = review.decide(proposal_id, req.status, req.note)
    if not changed:
        # Already decided. Not a 404 and not a 500: a second click on a stale page is a
        # normal thing to do, and the honest answer is "nothing changed".
        raise HTTPException(409, f"proposal {proposal_id} was already decided")
    return {"id": proposal_id, "status": req.status}


@app.post("/memory/proposals/subject")
def decide_subject(req: SubjectDecision) -> dict[str, object]:
    """Every pending claim about one subject. The common judgement really is per-subject —
    'this whole page is world history, not me' was the shape of the first real failure.
    There is no decide-everything route: a single button that approves the queue is the
    rubber stamp this whole mechanism exists to avoid."""
    from .memory import review

    return {"subject": req.subject, "status": req.status,
            "changed": review.decide_all(req.subject, req.status)}


@app.post("/memory/apply")
def apply_memory() -> dict[str, object]:
    """Write the approved claims. The ONLY path from a proposal to a page."""
    from .memory import build as build_mod

    try:
        return build_mod.apply_approved()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"{type(exc).__name__}: {exc}") from exc


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    """The single-page front end. Served from the package so there is nothing to build.

    The token is injected here rather than fetched from a route. A route that hands out the
    token would have to be unauthenticated to be useful, and then it is one more thing to
    reason about; injecting it means the page that can read this HTML is the page that has
    it, which is exactly the same-origin rule the browser already enforces.
    """
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return html.replace("__YOYO_TOKEN__", auth.read_or_create_token())


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return ui()


def port_is_free(host: str, port: int) -> bool:
    """Checked before uvicorn binds, so a clash is a sentence rather than a stack trace.

    Not a race-free reservation — something could take the port between this check and the
    bind. That is fine: the point is a readable message in the overwhelmingly common case,
    not a guarantee.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) != 0  # noqa: S104


def serve() -> None:
    import uvicorn

    s = get_settings()
    if not port_is_free(s.api_host, s.api_port):
        # Observed live: 8080 was already taken and uvicorn raised a bare OSError with a
        # winerror number. The fix is one env var and the user should not have to know that.
        raise SystemExit(
            f"Port {s.api_port} on {s.api_host} is already in use by something else.\n"
            f"Set YOYO_API_PORT in .env to a free port and run `yoyo serve` again."
        )
    auth.read_or_create_token()  # create it before the first request rather than during
    print(f"Yoyo UI:  http://{s.api_host}:{s.api_port}")  # noqa: T201
    print(f"API:      http://{s.api_host}:{s.api_port}/health\n")  # noqa: T201
    uvicorn.run(app, host=s.api_host, port=s.api_port, log_level=s.log_level.lower())
