"""The UI's write boundary, and the job runner behind it.

Two things are being defended here and they are easy to conflate.

**Auth** is not about keeping people out of a laptop-local service. It is about the fact
that any web page you visit can `fetch("http://127.0.0.1:8081/jobs", {method:"POST"})`, and
that loopback stops other *machines*, not other *origins*. These tests assert the property
that closes it — a state-changing request without the token fails — and, just as important,
that reads stay open, because a check that adds friction with no attacker to stop gets
switched off.

**Jobs** are about work that outlives a request. The properties that matter are: a failure
is a status rather than a crash, a reattaching client sees what it missed, and the record
keeps the *inputs* alongside the result. That last one is not tidiness — a bench number
without the host state it was taken on is what turned a wrong 1.09x into a three-day
misconception.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from yoyo import api, auth, jobs


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    monkeypatch.setattr(db_mod, "DEFAULT_PATH", path, raising=False)
    real = db_mod.connection
    monkeypatch.setattr(db_mod, "connection", lambda p=None: real(p or path))
    db_mod.migrate(path)
    return path


@pytest.fixture(autouse=True)
def temp_token(tmp_path, monkeypatch):
    token_file = tmp_path / "ui-token"
    monkeypatch.setattr(auth, "token_path", lambda: token_file)
    return auth.read_or_create_token(token_file)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    """The HTTP tests are about the boundary, not about execution.

    `jobs.start` is stubbed out so no worker thread outlives the test's temp database — a
    thread that finishes after teardown writes to a database that no longer exists, which
    surfaces as an unrelated error in whichever test runs next. The runner itself is
    exercised directly further down.
    """
    monkeypatch.setattr(jobs, "start", lambda job_id: None)
    return TestClient(api.app)


# --------------------------------------------------------------------- token ---


def test_a_token_is_created_once_and_reused(tmp_path):
    """Rotating on every start would log an open tab out on each restart, which trains the
    owner to reload rather than to notice."""
    path = tmp_path / "tok"
    first = auth.read_or_create_token(path)
    assert first and auth.read_or_create_token(path) == first


def test_the_token_is_long_enough_to_not_be_guessed(temp_token):
    assert len(temp_token) >= 32


def test_a_post_without_the_token_is_refused(client):
    """The property the whole file exists for. A page you visit must not be able to start
    work on your machine."""
    r = client.post("/jobs", json={"kind": "doctor"})
    assert r.status_code == 401
    assert jobs.recent() == []


def test_a_post_with_the_wrong_token_is_refused(client):
    r = client.post("/jobs", json={"kind": "doctor"},
                    headers={auth.TOKEN_HEADER: "not-the-token"})
    assert r.status_code == 401


def test_a_post_with_the_token_is_allowed(client, temp_token, monkeypatch):
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: {"fine": True})
    r = client.post("/jobs", json={"kind": "noop"}, headers={auth.TOKEN_HEADER: temp_token})
    assert r.status_code == 200
    assert r.json()["job_id"]


def test_reads_stay_open(client):
    """Gating GETs would add friction with no attacker to stop — a cross-origin page cannot
    read the response anyway. A check that annoys without protecting gets turned off."""
    assert client.get("/jobs").status_code == 200
    assert client.get("/stats").status_code == 200


def test_a_foreign_origin_is_rejected_even_with_a_token(client, temp_token):
    """Belt and braces. If a token ever leaks into a log or a screenshot, the Origin check
    is still standing between a hostile page and a POST."""
    r = client.post("/jobs", json={"kind": "doctor"},
                    headers={auth.TOKEN_HEADER: temp_token,
                             "origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_our_own_origin_is_accepted(client, temp_token, monkeypatch):
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: None)
    from yoyo.config import get_settings

    s = get_settings()
    r = client.post("/jobs", json={"kind": "noop"},
                    headers={auth.TOKEN_HEADER: temp_token,
                             "origin": f"http://127.0.0.1:{s.api_port}"})
    assert r.status_code == 200


def test_a_request_with_no_origin_is_allowed(client, temp_token, monkeypatch):
    """curl and the CLI send none, and they already have filesystem access to the token —
    they are not the threat this addresses."""
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: None)
    assert client.post("/jobs", json={"kind": "noop"},
                       headers={auth.TOKEN_HEADER: temp_token}).status_code == 200


def test_the_page_carries_the_token_and_leaves_no_placeholder(client):
    body = client.get("/").text
    assert "__YOYO_TOKEN__" not in body, "the token placeholder was not substituted"
    assert auth.read_or_create_token() in body


# ---------------------------------------------------------------------- jobs ---


def _wait(job_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = jobs.get(job_id)
        if row and row["status"] in {"done", "failed", "cancelled"}:
            return row
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_a_job_runs_and_records_its_result(monkeypatch):
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: {"answer": 42})
    job_id = jobs.create("noop", {"x": 1})
    jobs.start(job_id)
    row = _wait(job_id)
    assert row["status"] == "done"
    assert row["result"] == {"answer": 42}


def test_a_job_keeps_the_arguments_it_ran_with(monkeypatch):
    """Not tidiness. A bench number without the host state it was taken on is what made a
    wrong 1.09x survive three days as fact."""
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: None)
    job_id = jobs.create("noop", {"role": "supervisor", "host_note": "KEEP_ALIVE=30m"})
    jobs.start(job_id)
    assert _wait(job_id)["args"]["host_note"] == "KEEP_ALIVE=30m"


def test_a_failing_job_is_a_status_not_a_crash(monkeypatch):
    def boom(ctx):
        raise ValueError("the endpoint went away")

    monkeypatch.setitem(jobs.REGISTRY, "boom", boom)
    job_id = jobs.create("boom")
    jobs.start(job_id)
    row = _wait(job_id)
    assert row["status"] == "failed"
    assert "the endpoint went away" in row["error"]


def test_job_output_is_persisted_line_by_line(monkeypatch):
    def chatty(ctx):
        ctx.log("first")
        ctx.log("second")

    monkeypatch.setitem(jobs.REGISTRY, "chatty", chatty)
    job_id = jobs.create("chatty")
    jobs.start(job_id)
    assert _wait(job_id)["output"].splitlines() == ["first", "second"]


def test_subscribing_after_the_fact_replays_everything(monkeypatch):
    """A reload during a five-minute eval must show the lines that already happened, or it
    looks like nothing is running."""
    def chatty(ctx):
        ctx.log("alpha")
        ctx.log("beta")

    monkeypatch.setitem(jobs.REGISTRY, "chatty", chatty)
    job_id = jobs.create("chatty")
    jobs.start(job_id)
    _wait(job_id)

    events = list(jobs.subscribe(job_id))
    logs = [payload for kind, payload in events if kind == "log"]
    assert logs == ["alpha", "beta"]
    assert events[-1][0] == "done"


def test_a_cancelled_job_stops_at_its_next_checkpoint(monkeypatch):
    started = __import__("threading").Event()

    def slow(ctx):
        started.set()
        for _ in range(200):
            ctx.check_cancelled()
            time.sleep(0.01)
        return {"finished": True}

    monkeypatch.setitem(jobs.REGISTRY, "slow", slow)
    job_id = jobs.create("slow")
    jobs.start(job_id)
    assert started.wait(2.0)
    assert jobs.cancel(job_id) is True
    assert _wait(job_id)["status"] == "cancelled"


def test_cancelling_a_finished_job_is_not_an_error(monkeypatch):
    monkeypatch.setitem(jobs.REGISTRY, "noop", lambda ctx: None)
    job_id = jobs.create("noop")
    jobs.start(job_id)
    _wait(job_id)
    assert jobs.cancel(job_id) is False


def test_an_unknown_kind_is_refused_before_a_row_exists():
    """The registry is an allow-list. Without it, "the UI can run these seven things"
    quietly becomes "the UI can run any function in the process"."""
    with pytest.raises(jobs.JobError):
        jobs.create("rm-rf")
    assert jobs.recent() == []


def test_the_api_rejects_an_unknown_kind_with_422(client, temp_token):
    r = client.post("/jobs", json={"kind": "rm-rf"}, headers={auth.TOKEN_HEADER: temp_token})
    assert r.status_code == 422


def test_no_job_kind_is_destructive():
    """`restore` wipes the database. It is rare, irreversible, and belongs where you have to
    type it deliberately — not behind a button one click from a dashboard."""
    for banned in ("restore", "reindex-recreate", "memory-forget", "delete"):
        assert banned not in jobs.kinds()


def test_no_job_writes_a_memory_page():
    """A job proposes; only an explicit approval writes.

    There is no "build memory" job at all — the queue is the write path, and a button that
    wrote pages before the review existed would have made the review decorative. Asserted
    on the registry rather than on one function's source, so adding a writing job later
    fails here rather than passing quietly."""
    import inspect

    assert "memory-build" not in jobs.kinds()
    assert "memory-propose" in jobs.kinds()
    source = inspect.getsource(jobs.REGISTRY["memory-propose"])
    assert "propose_for_review" in source
    for writer in ("apply_approved", "write_page", "build_mod.build("):
        assert writer not in source


# ------------------------------------------------- the memory write boundary ---
# Exactly one route turns a proposal into a page. Everything else proposes.


def test_apply_is_the_only_route_that_writes_a_page():
    """Asserted on the route table rather than on prose, so a new writing route has to be
    added here deliberately instead of appearing quietly."""
    writing = [
        r.path for r in api.app.routes
        if getattr(r, "path", "").startswith("/memory")
        and "POST" in getattr(r, "methods", set())
    ]
    assert "/memory/apply" in writing
    assert sorted(writing) == ["/memory/apply", "/memory/proposals/subject",
                               "/memory/proposals/{proposal_id}"]


def test_deciding_a_proposal_needs_the_token(client):
    assert client.post("/memory/proposals/1", json={"status": "approved"}).status_code == 401


def test_applying_memory_needs_the_token(client):
    assert client.post("/memory/apply", json={}).status_code == 401


def test_reading_the_queue_does_not(client):
    assert client.get("/memory/proposals").status_code == 200


def test_deciding_an_already_decided_proposal_is_a_409_not_a_crash(client, temp_token):
    """A second click on a stale page is a normal thing to do. The honest answer is
    'nothing changed', not a 500."""
    from yoyo.memory import review
    from yoyo.memory.wiki import Claim

    review.propose([Claim(subject="Priya", kind="person", claim="c", quote="q",
                          source="conversation://1")])
    pid = review.pending()[0]["id"]
    headers = {auth.TOKEN_HEADER: temp_token}
    assert client.post(f"/memory/proposals/{pid}",
                       json={"status": "approved"}, headers=headers).status_code == 200
    assert client.post(f"/memory/proposals/{pid}",
                       json={"status": "rejected"}, headers=headers).status_code == 409


def test_an_invalid_decision_is_rejected_by_the_schema(client, temp_token):
    r = client.post("/memory/proposals/1", json={"status": "maybe"},
                    headers={auth.TOKEN_HEADER: temp_token})
    assert r.status_code == 422


def test_there_is_no_approve_everything_route():
    """A single button that clears the queue is the rubber stamp this whole mechanism
    exists to avoid. Per-claim and per-subject only."""
    paths = [getattr(r, "path", "") for r in api.app.routes]
    for banned in ("/memory/proposals/all", "/memory/approve-all", "/memory/proposals/approve"):
        assert banned not in paths
