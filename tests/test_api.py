"""HTTP API tests, with the model mocked.

`/ask/stream` had been written and never exercised — README §10 listed it under "assume
broken until exercised" for two days. It is also the seam a future phone client would talk
to (ADR-021 keeps business logic behind `api.py` precisely so that extraction stays cheap),
so an untested streaming path is an untested product boundary, not just an untested function.

Everything is mocked at `llm`, so these run offline. What they prove: the wiring, the
request contract, and that reasoning traces never reach the wire. What they do not prove:
that MyAIServer streams the way the client expects.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yoyo import api


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """A real, migrated SQLite file per test.

    Agent and graph turns are now persisted, so these tests touch the database where before
    they did not. Mocking the store would leave the persistence path untested at exactly the
    moment it started mattering; a temp file keeps it real and keeps the developer's own
    conversations out of the suite.
    """
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    monkeypatch.setattr(db_mod, "DEFAULT_PATH", path, raising=False)

    real_connection = db_mod.connection

    def scoped(p=None):
        return real_connection(p or path)

    monkeypatch.setattr(db_mod, "connection", scoped)
    db_mod.migrate(path)
    return path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api.app)


@pytest.fixture()
def no_rag(monkeypatch):
    """Retrieval off by default: these tests are about the HTTP layer, and a real retrieval
    would need an ingested corpus and a live Qdrant."""
    monkeypatch.setattr(api.rag, "retrieve", lambda *a, **k: [])
    from yoyo.rag import retrieve as rag_mod

    monkeypatch.setattr(rag_mod, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(rag_mod, "build_context", lambda passages: "")


def _stream(monkeypatch, pieces, captured: dict | None = None):
    from yoyo import llm

    def fake_stream(messages, role="answer", **kw):
        if captured is not None:
            captured["messages"] = list(messages)
            captured["role"] = role
        yield from pieces

    monkeypatch.setattr(llm, "stream_chat", fake_stream)


# ------------------------------------------------------------------- /ask/stream ---


def test_stream_returns_the_pieces_concatenated(client, monkeypatch, no_rag):
    _stream(monkeypatch, ["Hello", " ", "world"])
    r = client.post("/ask/stream", json={"question": "hi", "use_rag": False})
    assert r.status_code == 200
    assert r.text == "Hello world"


def test_stream_declares_a_streamable_content_type(client, monkeypatch, no_rag):
    _stream(monkeypatch, ["x"])
    r = client.post("/ask/stream", json={"question": "hi", "use_rag": False})
    assert r.headers["content-type"].startswith("text/plain")


def test_stream_sends_the_question_and_the_requested_role(client, monkeypatch, no_rag):
    captured: dict = {}
    _stream(monkeypatch, ["ok"], captured)
    client.post(
        "/ask/stream", json={"question": "what is the GB10", "role": "summarize", "use_rag": False}
    )
    assert captured["role"] == "summarize"
    assert any("what is the GB10" in str(m.get("content", "")) for m in captured["messages"])


def test_stream_always_opens_with_the_system_prompt(client, monkeypatch, no_rag):
    captured: dict = {}
    _stream(monkeypatch, ["ok"], captured)
    client.post("/ask/stream", json={"question": "hi", "use_rag": False})
    assert captured["messages"][0]["role"] == "system"


def test_stream_includes_retrieved_context_when_rag_is_on(client, monkeypatch):
    """The failure this catches is silent: streaming answers that look fine but were
    generated with no corpus behind them."""
    from yoyo.rag import retrieve as rag_mod

    monkeypatch.setattr(rag_mod, "retrieve", lambda *a, **k: ["fake-passage"])
    monkeypatch.setattr(rag_mod, "build_context", lambda p: "[1] the box is a GB10")
    captured: dict = {}
    _stream(monkeypatch, ["ok"], captured)

    client.post("/ask/stream", json={"question": "what box?", "use_rag": True})
    assert "the box is a GB10" in str(captured["messages"][-1]["content"])


def test_stream_omits_context_entirely_when_retrieval_finds_nothing(client, monkeypatch, no_rag):
    """An empty context block would still be sent as a header with nothing under it, which
    reads to the model as 'the corpus returned nothing relevant' — a different claim."""
    captured: dict = {}
    _stream(monkeypatch, ["ok"], captured)
    client.post("/ask/stream", json={"question": "hi", "use_rag": True})
    assert captured["messages"][-1]["content"] == "hi"


def test_an_empty_question_is_rejected_before_the_model_is_called(client, monkeypatch, no_rag):
    called = {"n": 0}

    from yoyo import llm

    def fake(*a, **k):
        called["n"] += 1
        yield "should not happen"

    monkeypatch.setattr(llm, "stream_chat", fake)
    assert client.post("/ask/stream", json={"question": ""}).status_code == 422
    assert called["n"] == 0


def test_a_stream_that_yields_nothing_is_an_empty_body_not_a_crash(client, monkeypatch, no_rag):
    _stream(monkeypatch, [])
    r = client.post("/ask/stream", json={"question": "hi", "use_rag": False})
    assert r.status_code == 200
    assert r.text == ""


def test_reasoning_traces_are_not_streamed_to_the_client():
    """Invariant 9: reasoning is not the answer. `stream_chat` is the only source of
    streamed text, and it must never be handed `reasoning_content`."""
    import inspect

    source = inspect.getsource(api.ask_stream)
    assert "reasoning" not in source.replace("Reasoning traces are dropped", "")


# -------------------------------------------------------------------- other routes ---


def test_search_returns_normalised_passages(client, monkeypatch):
    from yoyo.rag.retrieve import Passage

    monkeypatch.setattr(
        api.rag,
        "retrieve",
        lambda *a, **k: [
            Passage(chunk_id=7, title="MyAIServer", source_path="notes/a.md",
                    ordinal=0, score=0.9, text="the box")
        ],
    )
    r = client.get("/search", params={"q": "box"})
    assert r.status_code == 200
    assert r.json()[0]["chunk_id"] == 7


def test_health_reports_the_doctor_checks(client, monkeypatch):
    monkeypatch.setattr(
        api.doctor, "summary", lambda: {"ok": True, "env": {}, "checks": []}
    )
    assert client.get("/health").json()["ok"] is True


def test_the_api_binds_to_loopback_by_default():
    """Not a style point. ADR-021 makes the security boundary explicit; an API that
    defaulted to 0.0.0.0 would put the whole corpus on the LAN by accident."""
    from yoyo.config import get_settings

    assert get_settings().api_host in {"127.0.0.1", "localhost"}


def test_there_is_no_route_that_writes_to_the_vault_or_sends_mail():
    """The approval asymmetry has to hold at the HTTP boundary too, or a future phone client
    would route around it.

    Checks METHODS on vault and mail paths, not path names. An earlier version banned any
    route whose path contained "/vault" and failed the moment a read-only `/vault/graph` was
    added — the third time in this project a test policed a string instead of behaviour. The
    rule was never "no vault routes"; it is "no vault WRITES".
    """
    writes = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = []
    for route in api.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not (methods & writes):
            continue
        if any(word in path for word in ("/vault", "/mail", "/draft", "/send")):
            offenders.append(f"{sorted(methods)} {path}")
    assert not offenders, f"write routes onto vault or mail: {offenders}"


def test_the_vault_route_is_read_only():
    vault_routes = [r for r in api.app.routes if "/vault" in getattr(r, "path", "")]
    assert vault_routes, "the vault graph route disappeared"
    for route in vault_routes:
        assert set(route.methods) <= {"GET", "HEAD", "OPTIONS"}, route.path


# ---------------------------------------------------------------------- UI ----
# The UI exists for the things the CLI can show and nothing else could: tool calls as they
# arrive, citations you can click, and the fabricated-citation warning. These tests cover
# the plumbing behind those three, not the look of the page.


def _events(body: str) -> list[tuple[str, dict]]:
    import json as _json

    out = []
    for block in body.split("\n\n"):
        kind = body_line = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                body_line = line[6:]
        if kind and body_line:
            out.append((kind, _json.loads(body_line)))
    return out


def _fake_agent(monkeypatch, **kw):
    from yoyo import agent as agent_mod

    class FakeInv:
        def __init__(self, name, args, ok=True, error=None):
            self.name, self.arguments, self.ok, self.error = name, args, ok, error

    class FakeResult:
        text = kw.get("text", "The answer [7].")
        model = "coder"
        latency_ms = 1234
        iterations = 2
        stopped_because = "completed"
        fabricated_links = kw.get("fabricated_links", [])
        invocations = kw.get(
            "invocations", [FakeInv("mail_search", {"query": "x"})]
        )

        @property
        def tools_called(self):
            return [i.name for i in self.invocations]

    monkeypatch.setattr(agent_mod, "run", lambda *a, **k: FakeResult())


def test_the_agent_stream_emits_tool_calls_before_the_answer(client, monkeypatch):
    """The whole reason for a UI. If tools arrive after the answer they are a log, not a
    view of the work."""
    _fake_agent(monkeypatch)
    r = client.post("/agent/stream", json={"question": "q", "mode": "agent"})
    kinds = [k for k, _ in _events(r.text)]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert kinds.index("tool") < kinds.index("answer")


def test_a_failed_tool_call_is_reported_not_hidden(client, monkeypatch):
    class Inv:
        name, arguments, ok, error = "mail_search", {}, False, "auth expired"

    _fake_agent(monkeypatch, invocations=[Inv()])
    r = client.post("/agent/stream", json={"question": "q", "mode": "agent"})
    tool = next(d for k, d in _events(r.text) if k == "tool")
    assert tool["ok"] is False and "auth expired" in tool["error"]


def test_fabricated_links_reach_the_browser(client, monkeypatch):
    """Surfaced, never swallowed — same rule as the CLI. A turn needing the scrubber is a
    turn whose model invented a citation."""
    _fake_agent(monkeypatch, fabricated_links=["file:///"])
    r = client.post("/agent/stream", json={"question": "q", "mode": "agent"})
    answer = next(d for k, d in _events(r.text) if k == "answer")
    assert answer["fabricated_links"] == ["file:///"]


def test_an_exception_becomes_an_error_event_not_a_dead_stream(client, monkeypatch):
    from yoyo import agent as agent_mod

    def boom(*a, **k):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(agent_mod, "run", boom)
    r = client.post("/agent/stream", json={"question": "q", "mode": "agent"})
    kinds = dict(_events(r.text))
    assert "model unreachable" in kinds["error"]["message"]
    assert "done" in [k for k, _ in _events(r.text)]


def test_an_unknown_mode_is_rejected(client):
    assert client.post("/agent/stream", json={"question": "q", "mode": "wat"}).status_code == 422


def test_the_ui_is_served_and_is_self_contained():
    """No CDN. A UI that fetches a framework from unpkg breaks on the first offline day,
    which would quietly contradict the whole point of the project."""
    import re as _re
    from pathlib import Path as _P

    import yoyo

    html = (_P(yoyo.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "<title>Yoyo</title>" in html

    # Check what the page LOADS, not what it mentions. A first attempt banned the substring
    # "unpkg" anywhere and failed on the comment explaining why unpkg is not used — policing
    # prose again rather than behaviour.
    remote_attrs = _re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html)
    external = [u for u in remote_attrs if u.startswith(("http://", "https://", "//"))]
    assert not external, f"the UI loads remote resources: {external}"

    fetches = _re.findall(r"""fetch\(\s*["'`]([^"'`]+)""", html)
    remote_fetch = [u for u in fetches if u.startswith(("http://", "https://", "//"))]
    assert not remote_fetch, f"the UI fetches from outside: {remote_fetch}"

    assert "<script src=" not in html and "<link rel=\"stylesheet\"" not in html


def test_the_root_and_ui_routes_both_serve_the_page(client):
    for path in ("/", "/ui"):
        r = client.get(path)
        assert r.status_code == 200
        assert "<title>Yoyo</title>" in r.text


def test_a_chunk_citation_resolves(client, monkeypatch):
    class FakeConn:
        def execute(self, *a):
            class R:
                def fetchone(self_inner):
                    return (7, "chunk text", 0, "MyAIServer", "notes/a.md")
            return R()

    import contextlib

    from yoyo.storage import db as db_mod

    monkeypatch.setattr(db_mod, "connection", lambda: contextlib.nullcontext(FakeConn()))
    r = client.get("/citation/7")
    assert r.status_code == 200
    assert r.json()["kind"] == "chunk"
    assert r.json()["text"] == "chunk text"


def test_a_mail_citation_resolves(client, monkeypatch):
    from yoyo import mail as mail_mod
    from yoyo.mail.base import Message

    class FakeProvider:
        def read(self, mid):
            return Message(id=mid, account="personal", subject="Receipt", sender="a@b.c")

    monkeypatch.setattr(mail_mod, "resolve", lambda *a, **k: FakeProvider())
    r = client.get("/citation/mail:abc123")
    assert r.status_code == 200
    assert r.json()["kind"] == "mail"
    assert r.json()["citation"] == "mail:abc123"


def test_an_unresolvable_citation_is_a_404_not_a_crash(client, monkeypatch):
    from yoyo import vault as vault_mod

    monkeypatch.setattr(vault_mod, "read_note", lambda p: (_ for _ in ()).throw(
        vault_mod.VaultError("no such note")
    ))
    assert client.get("/citation/Nope.md").status_code == 404


# ------------------------------------------------------------------- serving ----


def test_the_default_port_is_not_the_one_everything_collides_on():
    """8080 was already taken on the owner's machine. Defaults should avoid the most
    contested port on a developer laptop."""
    from yoyo.config import Settings

    assert Settings.model_fields["api_port"].default == 8081


def test_a_free_port_reads_as_free():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert api.port_is_free("127.0.0.1", free) is True


def test_an_occupied_port_reads_as_occupied():
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        assert api.port_is_free("127.0.0.1", port) is False


def test_serve_refuses_a_busy_port_with_a_fix_not_a_traceback(monkeypatch):
    """A bare OSError with a winerror number tells the user nothing actionable."""
    monkeypatch.setattr(api, "port_is_free", lambda h, p: False)
    with pytest.raises(SystemExit) as exit_info:
        api.serve()
    message = str(exit_info.value)
    assert "already in use" in message
    assert "YOYO_API_PORT" in message


# ----------------------------------------------- MCP mounting (2026-08-15) -----
# Live bug from the UI: `web_search` was configured, enabled, and had a working SearXNG
# behind it, and the agent replied "unknown tool 'web_search'. Registered: corpus_stats,
# current_time, read_chunk, search_corpus". The CLI mounts MCP servers before every agent
# run; the API never did, so the browser only ever saw the four built-ins.


def test_the_app_mounts_mcp_servers_at_startup(monkeypatch):
    """At startup, not per request — each mount spawns a stdio subprocess, and doing that
    per question would add seconds to every turn and leak processes."""
    calls = {"mount": 0, "unmount": 0}

    def fake_mount_all():
        calls["mount"] += 1
        return {"mail": {"ok": True, "error": None}}

    monkeypatch.setattr(api.mcp_client, "mount_all", fake_mount_all)
    monkeypatch.setattr(api.mcp_client, "unmount_all",
                        lambda: calls.__setitem__("unmount", calls["unmount"] + 1))

    with TestClient(api.app) as c:
        c.get("/health")
        c.get("/health")
    assert calls["mount"] == 1, "mounted per request instead of once"
    assert calls["unmount"] == 1


def test_a_server_that_fails_to_mount_does_not_stop_the_app(monkeypatch):
    """One broken server must not take the UI down — the others still work, and the failure
    shows in /health rather than as a dead page."""
    monkeypatch.setattr(
        api.mcp_client, "mount_all",
        lambda: {"mail": {"ok": True, "error": None},
                 "search": {"ok": False, "error": "SearXNG unreachable"}},
    )
    monkeypatch.setattr(api.mcp_client, "unmount_all", lambda: None)
    with TestClient(api.app) as c:
        body = c.get("/health").json()
    assert body["mcp"] == {"mail": True, "search": False}


def test_health_reports_which_tools_the_model_can_reach(monkeypatch):
    """The missing fact in the live bug. "Registered: ..." was in the model's error message
    and nowhere the owner would look."""
    monkeypatch.setattr(api.mcp_client, "mount_all", lambda: {})
    monkeypatch.setattr(api.mcp_client, "unmount_all", lambda: None)
    monkeypatch.setattr(api.doctor, "summary", lambda: {"ok": True, "env": {}, "checks": []})
    with TestClient(api.app) as c:
        body = c.get("/health").json()
    assert "search_corpus" in body["tools"]


# ------------------------------------------ conversation memory (2026-08-15) ---
# The UI could show a conversation and lose it on a refresh, and a follow-up question
# ("and what about tomorrow?") reached the model with no idea what came before. The SQLite
# tables existed from the first migration; nothing ever passed a conversation_id.


def test_a_turn_creates_a_conversation_when_none_is_given(client, monkeypatch):
    _fake_agent(monkeypatch)
    r = client.post("/agent/stream", json={"question": "first", "mode": "agent"})
    start = next(d for k, d in _events(r.text) if k == "start")
    assert isinstance(start["conversation_id"], int)


def test_the_turn_is_persisted_and_readable_afterwards(client, monkeypatch):
    _fake_agent(monkeypatch, text="the answer")
    r = client.post("/agent/stream", json={"question": "what did Suno charge?", "mode": "agent"})
    cid = next(d for k, d in _events(r.text) if k == "start")["conversation_id"]

    body = client.get(f"/conversations/{cid}").json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assert body["messages"][0]["content"] == "what did Suno charge?"
    assert body["messages"][1]["content"] == "the answer"


def test_a_follow_up_reuses_the_conversation_rather_than_starting_one(client, monkeypatch):
    _fake_agent(monkeypatch)
    first = client.post("/agent/stream", json={"question": "one", "mode": "agent"})
    cid = next(d for k, d in _events(first.text) if k == "start")["conversation_id"]

    second = client.post(
        "/agent/stream", json={"question": "two", "mode": "agent", "conversation_id": cid}
    )
    assert next(d for k, d in _events(second.text) if k == "start")["conversation_id"] == cid
    assert len(client.get(f"/conversations/{cid}").json()["messages"]) == 4


def test_prior_turns_are_replayed_to_the_model(client, monkeypatch):
    """Without this the feature is cosmetic — the sidebar would show history the model
    could not see, which is worse than no history at all."""
    seen = {}
    from yoyo import agent as agent_mod

    class R:
        text, model, latency_ms, iterations = "ok", "coder", 1, 1
        stopped_because, fabricated_links, invocations = "completed", [], []
        tools_called: list = []

    def capture(question, **kw):
        seen["history"] = kw.get("history")
        return R()

    monkeypatch.setattr(agent_mod, "run", capture)
    first = client.post(
        "/agent/stream", json={"question": "what did Suno charge?", "mode": "agent"}
    )
    cid = next(d for k, d in _events(first.text) if k == "start")["conversation_id"]
    client.post("/agent/stream",
                json={"question": "and the month before?", "mode": "agent", "conversation_id": cid})

    contents = [m["content"] for m in seen["history"]]
    assert "what did Suno charge?" in contents


def test_the_conversation_is_titled_from_the_first_question(client, monkeypatch):
    """Nobody names a conversation before having one."""
    _fake_agent(monkeypatch)
    r = client.post("/agent/stream", json={"question": "what did Suno charge me?", "mode": "agent"})
    cid = next(d for k, d in _events(r.text) if k == "start")["conversation_id"]
    listed = {c["id"]: c for c in client.get("/conversations").json()}
    assert listed[cid]["title"] == "what did Suno charge me?"


def test_a_very_long_question_produces_a_sidebar_sized_title():
    from yoyo import core as core_mod

    title = core_mod.title_for("word " * 200)
    assert len(title) <= core_mod.TITLE_CHARS + 1
    assert title.endswith("…")


def test_conversations_are_listed_most_recent_first(client, monkeypatch):
    _fake_agent(monkeypatch)
    for q in ("older", "newer"):
        client.post("/agent/stream", json={"question": q, "mode": "agent"})
    titles = [c["title"] for c in client.get("/conversations").json()]
    assert titles[0] == "newer"


def test_a_missing_conversation_is_a_404(client):
    assert client.get("/conversations/999999").status_code == 404


def test_a_conversation_can_be_deleted(client, monkeypatch):
    _fake_agent(monkeypatch)
    r = client.post("/agent/stream", json={"question": "throwaway", "mode": "agent"})
    cid = next(d for k, d in _events(r.text) if k == "start")["conversation_id"]
    assert client.delete(f"/conversations/{cid}").status_code == 200
    assert cid not in {c["id"] for c in client.get("/conversations").json()}


def test_tool_calls_are_recorded_so_a_reloaded_turn_still_shows_them(client, monkeypatch):
    _fake_agent(monkeypatch)
    r = client.post("/agent/stream", json={"question": "q", "mode": "agent"})
    cid = next(d for k, d in _events(r.text) if k == "start")["conversation_id"]
    reply = client.get(f"/conversations/{cid}").json()["messages"][1]
    assert reply["metadata"]["tools"] == ["mail_search"]
    assert reply["metadata"]["mode"] == "agent"


# ------------------------------------------------- vault graph (2026-08-15) ----
# Built after a live turn answered "your notes describe the GB10…" citing a CORPUS document,
# while the vault held one empty file. Vault and corpus are different stores that drift, and
# nothing made that visible. The corpus overlay is the point of this view.


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "Notes"
    root.mkdir()
    (root / "GB10.md").write_text(
        "# GB10\n\nSee [[MyAIServer]] and [[Unwritten]].\n", encoding="utf-8"
    )
    (root / "MyAIServer.md").write_text("# Server\n\nBack to [[GB10]].\n", encoding="utf-8")
    (root / "scratch.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("YOYO_VAULT_PATH", str(root))
    return root


def test_the_graph_returns_notes_and_links(client, vault):
    g = client.get("/vault/graph").json()
    titles = {n["title"] for n in g["nodes"]}
    assert {"GB10", "MyAIServer", "scratch"} <= titles
    assert g["stats"]["links"] == 3


def test_a_link_to_an_unwritten_note_is_kept_as_a_node(client, vault):
    """Obsidian shows these, and dropping them would hide the shape of what you meant to
    write."""
    g = client.get("/vault/graph").json()
    missing = [n for n in g["nodes"] if not n["exists"]]
    assert [n["title"] for n in missing] == ["Unwritten"]
    assert g["stats"]["missing"] == 1


def test_an_empty_note_is_flagged(client, vault):
    g = client.get("/vault/graph").json()
    scratch = next(n for n in g["nodes"] if n["title"] == "scratch")
    assert scratch["empty"] is True
    assert g["stats"]["empty"] == 1


def test_corpus_membership_is_reported_per_note(client, vault, monkeypatch):
    """The distinction the agent got wrong: a note in the vault is not necessarily in the
    corpus, and corpus content is not "your notes"."""
    from yoyo import vault as vault_mod

    monkeypatch.setattr(vault_mod, "_ingested_stems", lambda: {"gb10"})
    g = client.get("/vault/graph").json()
    by_title = {n["title"]: n for n in g["nodes"]}
    assert by_title["GB10"]["in_corpus"] is True
    assert by_title["MyAIServer"]["in_corpus"] is False
    assert g["stats"]["in_corpus"] == 1


def test_the_overlay_degrades_rather_than_failing_without_a_database(monkeypatch, vault):
    """The graph is still worth drawing without the corpus overlay."""
    from yoyo import vault as vault_mod
    from yoyo.storage import db as db_mod

    monkeypatch.setattr(db_mod, "connection", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no database")))
    assert vault_mod._ingested_stems() == set()
    assert vault_mod.graph()["stats"]["notes"] == 3


def test_no_vault_configured_is_a_404_not_a_crash(client, monkeypatch):
    monkeypatch.delenv("YOYO_VAULT_PATH", raising=False)
    from yoyo import vault as vault_mod

    monkeypatch.setattr(vault_mod, "graph", lambda folder="": (_ for _ in ()).throw(
        vault_mod.VaultError("No vault configured")))
    assert client.get("/vault/graph").status_code == 404


def test_the_map_is_drawn_without_a_charting_library():
    """Same rule as the rest of the UI: no CDN, no build step. A force layout for a personal
    vault is about forty lines."""
    from pathlib import Path as _P

    import yoyo

    html = (_P(yoyo.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "drawGraph" in html
    for lib in ("d3.", "cytoscape", "vis-network", "chart.js"):
        assert lib not in html.lower()
