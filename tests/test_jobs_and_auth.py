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


# ------------------------------------------------------------- the tool guide ---


def test_every_built_in_tool_has_a_guide_entry():
    """The page exists because "34 tools" appeared in the corner with no way to find out
    which 34. A tool with no entry recreates that gap one level down, so at minimum the
    ones this project ships must be documented."""
    from yoyo import toolguide
    from yoyo import tools as tools_mod

    missing = [n for n in tools_mod.registry.names() if toolguide.entry_for(n) is None]
    assert not missing, f"tools with no guide entry: {missing}"


def test_an_undocumented_tool_is_surfaced_not_hidden():
    from yoyo import toolguide

    described = toolguide.describe("some_third_party_tool")
    assert described["documented"] is False
    assert "Not yet documented" in described["group"]
    assert any(g["name"] == "Not yet documented" for g in toolguide.group_order())


def test_the_guide_matches_a_tool_whose_server_was_renamed():
    """MCP tools arrive prefixed with their server name, and that prefix is config. Renaming
    a server in yoyo-mcp.yaml must not silently empty the guide."""
    from yoyo import toolguide

    assert toolguide.entry_for("notes_search") is toolguide.entry_for("vault_search")


def test_the_guide_never_invents_a_tool():
    """The registry is the source of truth for what exists; the guide only adds prose. A
    guide entry for a tool you do not have is the doc-drift failure this project already
    fails a test over."""
    from fastapi.testclient import TestClient

    from yoyo import api as api_mod
    from yoyo import tools as tools_mod

    listed = {t["name"] for t in TestClient(api_mod.app).get("/tools").json()["tools"]}
    assert listed == set(tools_mod.registry.names())


def test_the_tools_route_reports_which_have_no_guide(client):
    body = client.get("/tools").json()
    assert "undocumented" in body
    assert "groups" in body and body["groups"]


# --------------------------------------------------- route order (live bug) ---


def test_deciding_a_whole_subject_is_not_swallowed_by_the_id_route(client, temp_token):
    """The bug the owner hit clicking "keep all".

    FastAPI matches routes in declaration order. With `/memory/proposals/{proposal_id}`
    declared first, a POST to `/memory/proposals/subject` was captured by the parameterised
    route, "subject" failed to parse as an int, and the browser got a 422 whose `detail` is a
    LIST of validation objects — rendered as "[object Object]". Specific before parameterised.
    """
    from yoyo.memory import review
    from yoyo.memory.wiki import Claim

    review.propose([
        Claim(subject="Janam", kind="person", claim="is my son and in 9th grade",
              quote="my son's name is Janam, he is in 9th grade", source="conversation://2"),
        Claim(subject="Janam", kind="person", claim="second fact",
              quote="my son Janam plays cricket", source="conversation://2"),
    ])

    r = client.post("/memory/proposals/subject",
                    json={"subject": "Janam", "status": "approved"},
                    headers={auth.TOKEN_HEADER: temp_token})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2
    assert review.pending() == []


def test_the_literal_route_is_declared_before_the_parameterised_one():
    """Structural, so re-adding a route above it fails here rather than in the browser."""
    paths = [getattr(r, "path", "") for r in api.app.routes]
    assert paths.index("/memory/proposals/subject") < paths.index("/memory/proposals/{proposal_id}")


def test_a_numeric_id_still_reaches_the_per_claim_route(client, temp_token):
    """The fix must not shadow the route it was ordered ahead of."""
    from yoyo.memory import review
    from yoyo.memory.wiki import Claim

    review.propose([Claim(subject="Bhavin", kind="person", claim="name",
                          quote="my name is Bhavin", source="conversation://2")])
    pid = review.pending()[0]["id"]
    r = client.post(f"/memory/proposals/{pid}", json={"status": "approved"},
                    headers={auth.TOKEN_HEADER: temp_token})
    assert r.status_code == 200
    assert r.json()["id"] == pid


# ------------------------------------------------- stale code (the meta-bug) ---


def test_health_reports_when_the_running_code_is_older_than_the_disk(client, monkeypatch):
    """The bug that caused a second bug report for an already-fixed bug.

    `ui()` re-reads `index.html` on every request; Python modules are imported once. Update
    the files without restarting and the new front end talks to the old backend — which is
    exactly how a fixed route-ordering bug kept reproducing. The server can see this, so it
    has to say it.
    """
    monkeypatch.setattr(api, "LOADED_SOURCE_MTIME", 0.0)
    assert client.get("/health").json()["stale_code"] is True


def test_fresh_code_is_not_reported_as_stale(client, monkeypatch):
    monkeypatch.setattr(api, "LOADED_SOURCE_MTIME", 2_000_000_000.0)
    assert client.get("/health").json()["stale_code"] is False


def test_a_hair_of_clock_skew_does_not_cry_wolf(monkeypatch):
    """A banner that fires on a half-second of copy latency gets ignored like any other."""
    monkeypatch.setattr(api, "_newest_source_mtime", lambda: api.LOADED_SOURCE_MTIME + 0.4)
    assert api.code_is_stale() is False
