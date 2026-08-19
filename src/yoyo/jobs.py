"""Long-running work, started from the UI and survivable across a page reload.

`yoyo eval` is five minutes. `bench --repeats 3` is longer. `ingest` over a real folder,
`reindex`, `memory build` — all minutes, none of which fit in an HTTP request/response, and
none of which should die because a browser tab was closed.

So: a job is a row. Start one, get an id, stream its output; reload the page and reattach by
id. Three properties matter more than the mechanism.

**Jobs are history, not just progress.** Once an eval run is a row with a result, "did case
3 pass" becomes "case 3 has flipped twice this week". The 1.09x → 3.75x concurrency reversal
took three days to notice because each bench run was a screenshot; as rows next to the host
configuration they were taken on, it would have been one glance.

**A job records what it ran on, not only what it produced.** `args` is stored verbatim. A
measurement without its inputs is an anecdote — that is the lesson the concurrency reversal
actually taught, and it is cheaper to enforce here than to remember.

**Failure is a status, never an exception the caller has to catch.** A job that raises ends
`failed` with the traceback in `error`, and the UI shows it. Silently-disappeared work is
the failure mode that makes people distrust a queue.

Threads, not processes: everything here is IO-bound on MyAIServer, and a thread can share
the SQLite connection helper and the already-mounted MCP servers. The cap exists so that
clicking "run eval" three times does not put three tool loops in contention for one box.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any

from .storage import db

log = logging.getLogger(__name__)

#: One at a time by default. These jobs all hit the same single GPU box; running two evals
#: concurrently does not halve the wall clock, it doubles both and corrupts the timings.
MAX_CONCURRENT = 1

#: How long a stream waits for a line before emitting a keep-alive. Without this a proxy or
#: a browser will close an idle SSE connection during a long model call, and the UI shows a
#: silent stall exactly when the work is going fine.
HEARTBEAT_S = 15.0


class JobError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """Raised inside a job when the owner asks it to stop."""


@dataclass
class JobContext:
    """What a job body is handed: a way to talk, and a way to be stopped."""

    job_id: int
    args: dict[str, Any]
    _emit: Callable[[str], None]
    _cancel: threading.Event

    def log(self, line: str) -> None:
        self._emit(line.rstrip("\n"))

    def check_cancelled(self) -> None:
        """Call between units of work. Cooperative, because there is no safe way to kill a
        thread mid-HTTP-request without leaking the connection."""
        if self._cancel.is_set():
            raise Cancelled("cancelled by the owner")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


#: kind -> callable(ctx) -> result. Registered rather than dispatched by name lookup into
#: module globals: an explicit registry is the difference between "the UI can run these
#: seven things" and "the UI can run any function in the process".
REGISTRY: dict[str, Callable[[JobContext], Any]] = {}


def register(kind: str) -> Callable[[Callable[[JobContext], Any]], Callable[[JobContext], Any]]:
    def wrap(fn: Callable[[JobContext], Any]) -> Callable[[JobContext], Any]:
        REGISTRY[kind] = fn
        return fn

    return wrap


def kinds() -> list[str]:
    return sorted(REGISTRY)


# ------------------------------------------------------------------- storage ---


def create(kind: str, args: dict[str, Any] | None = None) -> int:
    if kind not in REGISTRY:
        raise JobError(f"unknown job kind {kind!r}. Known: {', '.join(kinds())}")
    with db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, args) VALUES (?, ?)",
            (kind, json.dumps(args or {}, default=str)),
        )
        return int(cur.lastrowid)


def get(job_id: int) -> dict[str, Any] | None:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _as_dict(row) if row else None


def recent(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM jobs"
    params: list[Any] = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db.connection() as conn:
        return [_as_dict(r) for r in conn.execute(sql, params).fetchall()]


def _as_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("args", "result"):
        raw = out.get(key)
        if raw:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                pass  # keep the raw text rather than losing it
    return out


def _update(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db.connection() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def _append_output(job_id: int, line: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE jobs SET output = output || ? WHERE id = ?", (line + "\n", job_id)
        )


# ------------------------------------------------------------------- running ---

#: Live in-process state for running jobs. The DB is the record; this is the wiring that
#: lets a stream see lines as they happen rather than polling the row.
_LIVE: dict[int, dict[str, Any]] = {}
_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


def start(job_id: int) -> None:
    """Run a job on a worker thread. Returns immediately."""
    row = get(job_id)
    if row is None:
        raise JobError(f"no job {job_id}")
    if row["status"] != "queued":
        raise JobError(f"job {job_id} is {row['status']}, not queued")

    cancel = threading.Event()
    subscribers: list[Queue] = []
    with _LOCK:
        _LIVE[job_id] = {"cancel": cancel, "subscribers": subscribers}

    def emit(line: str) -> None:
        _append_output(job_id, line)
        with _LOCK:
            for q in list(subscribers):
                q.put(("log", line))

    def finish(status: str, **fields: Any) -> None:
        _update(job_id, status=status, finished_at=_now(), **fields)
        with _LOCK:
            for q in list(subscribers):
                q.put(("done", {"status": status, **fields}))
            _LIVE.pop(job_id, None)

    def work() -> None:
        acquired = _SLOTS.acquire(timeout=3600)
        if not acquired:
            finish("failed", error="timed out waiting for a slot")
            return
        try:
            if cancel.is_set():
                # Cancelled while queued behind another job. Not an error.
                finish("cancelled")
                return
            _update(job_id, status="running", started_at=_now())
            with _LOCK:
                for q in list(subscribers):
                    q.put(("status", {"status": "running"}))
            ctx = JobContext(job_id=job_id, args=row["args"] or {}, _emit=emit, _cancel=cancel)
            result = REGISTRY[row["kind"]](ctx)
            finish("done", result=json.dumps(result, default=str) if result is not None else None)
        except Cancelled:
            finish("cancelled")
        except Exception as exc:  # noqa: BLE001 - a job's failure is data, not a crash
            log.exception("job %s (%s) failed", job_id, row["kind"])
            finish("failed", error=f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        finally:
            _SLOTS.release()

    threading.Thread(target=work, name=f"yoyo-job-{job_id}", daemon=True).start()


def cancel(job_id: int) -> bool:
    """Ask a job to stop. Cooperative — it stops at its next `check_cancelled()`.

    Returns False for a job that is not running, which includes one that finished a
    millisecond ago. That is not an error worth raising over.
    """
    with _LOCK:
        live = _LIVE.get(job_id)
    if not live:
        return False
    live["cancel"].set()
    return True


def subscribe(job_id: int) -> Iterator[tuple[str, Any]]:
    """Yield (event, payload) for a job: replayed history, then live events.

    Replay first, always. A UI that attaches after a job has produced 200 lines must see
    those 200 lines — otherwise reloading the page during a long eval shows an empty box and
    looks like nothing is happening.
    """
    row = get(job_id)
    if row is None:
        yield ("error", {"message": f"no job {job_id}"})
        return

    yield ("status", {"status": row["status"], "kind": row["kind"], "args": row["args"]})
    for line in (row["output"] or "").splitlines():
        yield ("log", line)

    if row["status"] not in {"queued", "running"}:
        yield ("done", {"status": row["status"], "result": row.get("result"),
                        "error": row.get("error")})
        return

    q: Queue = Queue()
    with _LOCK:
        live = _LIVE.get(job_id)
        if live is None:
            # Finished between the read above and here. Re-read rather than hang.
            fresh = get(job_id) or {}
            yield ("done", {"status": fresh.get("status", "done"),
                            "result": fresh.get("result"), "error": fresh.get("error")})
            return
        live["subscribers"].append(q)

    try:
        while True:
            try:
                event, payload = q.get(timeout=HEARTBEAT_S)
            except Empty:
                yield ("ping", {})
                continue
            yield (event, payload)
            if event == "done":
                return
    finally:
        with _LOCK:
            live = _LIVE.get(job_id)
            if live and q in live["subscribers"]:
                live["subscribers"].remove(q)


def _now() -> str:
    with db.connection() as conn:
        return str(conn.execute("SELECT datetime('now') AS t").fetchone()["t"])


# ----------------------------------------------------------------- the kinds ---
# Deliberately a short list. Every entry is something worth watching for minutes; anything
# instant belongs in a plain route, and anything irreversible (`restore`) belongs in a
# terminal where you have to type it.


@register("doctor")
def _job_doctor(ctx: JobContext) -> dict[str, Any]:
    from . import doctor as doctor_mod

    checks = doctor_mod.run_all()
    for c in checks:
        ctx.log(f"{'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    return {
        "ok": all(c.ok for c in checks),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }


@register("eval")
def _job_eval(ctx: JobContext) -> dict[str, Any]:
    from . import evals

    only = ctx.args.get("only")
    role = ctx.args.get("role")

    def on_start(i: int, total: int, case: dict) -> None:
        ctx.check_cancelled()
        ctx.log(f"({i}/{total}) {case['id']} [{case['kind']}] …")

    def on_result(i: int, total: int, result: Any) -> None:
        ctx.log(f"    {'PASS' if result.passed else 'FAIL'}  {result.detail}")

    report = evals.run(
        only=[only] if only else None, role_override=role,
        on_start=on_start, on_result=on_result,
    )
    ctx.log(f"{report.passed}/{report.total} passed")
    return {
        "passed": report.passed, "total": report.total, "ok": report.ok,
        "role": role,
        "cases": [{"id": r.case_id, "kind": r.kind, "passed": r.passed,
                   "detail": r.detail, "latency_ms": r.latency_ms} for r in report.results],
    }


@register("bench")
def _job_bench(ctx: JobContext) -> dict[str, Any]:
    from . import bench as bench_mod

    role = ctx.args.get("role", "supervisor")
    levels = tuple(int(n) for n in (ctx.args.get("concurrency") or [1, 4]))
    repeats = int(ctx.args.get("repeats", 3))

    ctx.log(f"role {role} · concurrency {list(levels)} · repeats {repeats}")
    ctx.check_cancelled()
    # bench.run() is one blocking call across all levels, so cancellation cannot land
    # mid-measurement. That is correct rather than a limitation: a half-measured level is
    # not a cheaper measurement, it is a wrong one.
    result = bench_mod.run(role=role, concurrencies=levels, repeats=repeats)

    rows = []
    for level in result.levels:
        row = {
            "concurrency": level.concurrency,
            "aggregate_tok_s": round(level.aggregate_tok_s, 1),
            "per_stream_tok_s": round(level.per_stream_tok_s, 1),
            "scaling": round(result.scaling(level), 2) if level.concurrency != 1 else None,
            "wall_clock_s": round(level.wall_clock_s, 1),
            "rate_limited": level.rate_limited,
        }
        rows.append(row)
        ctx.log(f"    {row}")
    ctx.log(result.verdict())

    # The host state a scaling number was taken on is PART of the number. `host_note` is
    # free text and is stored verbatim — the direct lesson of the 1.09x -> 3.75x reversal,
    # where nothing recorded that OLLAMA_KEEP_ALIVE had changed between the two runs.
    return {"role": role, "endpoint": result.endpoint, "repeats": repeats,
            "usable": result.usable, "verdict": result.verdict(), "rows": rows,
            "host_note": ctx.args.get("host_note", "")}


@register("ingest")
def _job_ingest(ctx: JobContext) -> dict[str, Any]:
    from pathlib import Path

    from .rag import ingest as ingest_mod

    target = Path(str(ctx.args.get("path", ""))).expanduser()
    if not target.exists():
        raise JobError(f"no such path: {target}")
    ctx.log(f"ingesting {target} …")
    report = ingest_mod.ingest_path(target, recursive=bool(ctx.args.get("recursive", True)))
    ctx.log(report.summary())
    for src, err in report.failed:
        ctx.log(f"FAILED {src}: {err}")
    return {"summary": report.summary(), "failed": [list(f) for f in report.failed]}


@register("remember")
def _job_remember(ctx: JobContext) -> dict[str, Any]:
    from .memory import sources as sources_mod

    report = sources_mod.remember(min_turns=int(ctx.args.get("min_turns", 1)))
    ctx.log(report.summary())
    return {"summary": report.summary()}


@register("research")
def _job_research(ctx: JobContext) -> dict[str, Any]:
    """Deep research on a topic: plan, gather, read, write, save a draft.

    A job rather than a request because it is minutes of work — several searches, a dozen
    page fetches and a long write — and because the log IS the audit trail. A research
    report that appears without showing what it read is a report you have to take on trust.
    """
    from . import research as research_mod

    topic = str(ctx.args.get("topic", "")).strip()
    if not topic:
        raise JobError("research needs a topic")
    depth = str(ctx.args.get("depth", research_mod.DEFAULT_DEPTH))

    report = research_mod.run(topic, depth=depth,
                              use_corpus=bool(ctx.args.get("corpus", True)),
                              on_log=ctx.log)
    ctx.check_cancelled()

    if ctx.args.get("save", True):
        try:
            path = research_mod.save_draft(report)
            ctx.log(f"saved draft: {path}")
        except Exception as exc:  # noqa: BLE001 - no vault is a normal state
            ctx.log(f"could not save a draft ({exc}) — the report is in this result")

    return {
        "topic": report.topic, "depth": report.depth, "questions": report.questions,
        "text": report.text, "summary": report.summary(),
        "sources": [s.as_dict() for s in report.sources],
        "invented_links": report.invented_links,
        "errors": report.errors, "draft_path": report.draft_path,
        "latency_ms": report.latency_ms,
    }


@register("memory-sweep")
def _job_memory_sweep(ctx: JobContext) -> dict[str, Any]:
    """The automatic path: idle conversations become proposals, incrementally.

    Queued by the scheduler rather than by a person, which is the whole point — but it runs
    through the same job runner as everything else, so an automatic sweep has a log, a
    duration and a visible failure. Work that happens invisibly cannot be debugged, and a
    memory system quietly not running looks exactly like one with nothing to say.
    """
    from .memory import pipeline

    reason = ctx.args.get("reason", "manual")
    ctx.log(f"sweep triggered by: {reason}")
    report = pipeline.sweep(on_log=ctx.log)
    return {
        "reason": reason, "considered": report.considered, "swept": report.swept,
        "queued": report.queued, "capped": report.capped,
        "skipped_capped": report.skipped_capped,
        "pending": report.pending_after,
        "failures": [list(f) for f in report.failures],
    }


@register("memory-propose")
def _job_memory_propose(ctx: JobContext) -> dict[str, Any]:
    """Extract, verify, and queue for review. Writes nothing to the vault.

    There is deliberately no "write memory" job. The gates decide what is traceable; you
    decide what is worth keeping, and the first real run is why that is not a formality —
    six pages of world history passed every gate. Approval happens in the Memory tab, and
    `POST /memory/apply` is the only path from a proposal to a page.
    """
    from . import core
    from . import vault as vault_mod
    from .memory import build as build_mod
    from .memory import sources as source_mod

    raw: dict[str, str] = {}
    for row in core.list_conversations(limit=1000):
        cid = int(row["id"])
        text, turns, _ = source_mod.render(cid, row.get("title"),
                                           core.conversation_messages(cid))
        if turns:
            raw[source_mod.source_path(cid)] = text
    if ctx.args.get("notes", True):
        root = vault_mod.vault_root()
        for note in vault_mod._notes(root):
            raw[f"vault://{vault_mod._rel(note, root)}"] = note.read_text(
                encoding="utf-8", errors="replace")

    ctx.log(f"reading {len(raw)} raw source(s) …")
    ctx.check_cancelled()
    report = build_mod.propose_for_review(raw)
    ctx.log(f"accepted={report['accepted']} queued={report['queued']} "
            f"already_pending={report['already_pending']} "
            f"previously_decided={report['previously_decided']}")
    for subject, why in report["rejected"][:10]:
        ctx.log(f"rejected  {subject}: {why}")
    for question in report["ambiguities"]:
        ctx.log(f"?  {question.get('question', question)}")
    if not report["queued"]:
        ctx.log("Nothing new to review. For real conversations that is a finding, "
                "not a bug — read it as 'no durable facts here'.")
    return report


@register("backup")
def _job_backup(ctx: JobContext) -> dict[str, Any]:
    from pathlib import Path

    from . import backup as backup_mod

    dest = Path(str(ctx.args.get("dest", ""))).expanduser()
    if not str(dest):
        raise JobError("backup needs a destination folder")
    archive = backup_mod.create(dest)
    ctx.log(f"wrote {archive}")
    ctx.check_cancelled()
    # Verified in the same job, always. An unverified backup is a guess, and making the
    # drill a separate button people forget to press is how you find out at the worst time.
    result = backup_mod.restore_drill(archive)
    for name, ok, detail in result.checks:
        ctx.log(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    ctx.log("drill passed" if result.ok else "DRILL FAILED — this archive is not a backup")
    return {"archive": str(archive), "drill_ok": bool(result.ok),
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in result.checks]}
