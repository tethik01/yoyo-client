"""Phase 1 of memory — conversations as verbatim raw sources.

The safety argument for everything that comes later rests on this layer being *dumb*. It
stores what was said and interprets nothing. When the wiki layer starts writing pages
automatically, every claim on a page must quote a raw source, and a quote that cannot be
found here is a quote that was invented. So these tests care most about two things:
verbatimness, and that a retrieved fragment says who was speaking.
"""

from __future__ import annotations

import pytest

from yoyo.memory import sources


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    real = db_mod.connection
    monkeypatch.setattr(db_mod, "connection", lambda p=None: real(p or path))
    db_mod.migrate(path)
    return path


MESSAGES = [
    {"role": "user", "content": "We decided to promote coder to the supervisor role.",
     "created_at": "2026-08-15 10:00:00"},
    {"role": "assistant", "content": "Recorded — coder passed all four gates 7/7.",
     "created_at": "2026-08-15 10:00:31"},
    {"role": "user", "content": "ok", "created_at": "2026-08-15 10:01:00"},
]


# ------------------------------------------------------------------ rendering ---


def test_the_transcript_is_verbatim():
    """Not a summary. If this ever paraphrases, every downstream provenance check becomes a
    check against text a model wrote."""
    text, turns, _ = sources.render(3, "model promotion", MESSAGES)
    assert "We decided to promote coder to the supervisor role." in text
    assert "coder passed all four gates 7/7." in text
    assert turns == 2


def test_each_turn_says_who_was_speaking():
    """A chunk reading "I decided to use coder" is useless if you cannot tell whether the
    owner said it or the model did — and that ambiguity is how a model's guess gets quoted
    back as the owner's decision."""
    text, _, _ = sources.render(3, None, MESSAGES)
    assert "## Bhavin" in text
    assert "## Yoyo" in text


def test_turns_carry_their_timestamp():
    text, _, _ = sources.render(3, None, MESSAGES)
    assert "2026-08-15 10:00:00" in text


def test_trivial_turns_are_skipped():
    """"ok", "thanks", "yes" are retrievable noise — they dilute the corpus and are never the
    answer to anything."""
    _, turns, skipped = sources.render(3, None, MESSAGES)
    assert turns == 2 and skipped == 1


def test_the_document_declares_its_own_source():
    text, _, _ = sources.render(7, None, MESSAGES)
    assert "conversation://7" in text
    assert "verbatim" in text


def test_system_and_tool_messages_never_appear():
    """A system prompt in the corpus would be retrieved and quoted as though the owner had
    written it."""
    text, turns, _ = sources.render(1, None, [
        {"role": "system", "content": "You are Yoyo, a personal assistant running locally."},
        {"role": "tool", "content": "{'result': 'x'}"},
        {"role": "user", "content": "What did the bake-off conclude about concurrency?"},
    ])
    assert "You are Yoyo" not in text
    assert "'result'" not in text
    assert turns == 1


# ------------------------------------------------------------------ identity ---


def test_source_paths_round_trip():
    assert sources.conversation_id_from(sources.source_path(42)) == 42


def test_a_non_conversation_path_is_not_claimed():
    assert sources.conversation_id_from("notes/GB10.md") is None
    assert not sources.is_conversation_document("notes/GB10.md")
    assert sources.is_conversation_document("conversation://1")


def test_a_malformed_conversation_path_is_none_not_a_crash():
    assert sources.conversation_id_from("conversation://not-a-number") is None


# ------------------------------------------------------------------- ingest ---


def _have(cid: int, question: str, answer: str) -> int:
    from yoyo import core

    core.persist_turn(cid, question, answer, model="coder", latency_ms=1)
    return cid


def test_conversations_become_searchable_documents(monkeypatch):
    from yoyo import core

    cid = core.new_conversation()
    core.persist_turn(cid, "What did Suno charge me in August?",
                      "11.30 dollars for the Pro subscription.", model="coder")

    written = {}
    from yoyo.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "ingest_text",
                        lambda **kw: written.update(kw) or True)

    report = sources.remember()
    assert report.conversations == 1
    assert report.written == 1
    assert written["source_path"] == f"conversation://{cid}"
    assert "What did Suno charge me in August?" in written["text"]


def test_re_running_reports_unchanged(monkeypatch):
    from yoyo import core
    from yoyo.rag import ingest as ingest_mod

    cid = core.new_conversation()
    core.persist_turn(cid, "a question long enough to survive the filter", "an answer likewise")

    monkeypatch.setattr(ingest_mod, "ingest_text", lambda **kw: False)
    report = sources.remember()
    assert report.unchanged == 1 and report.written == 0


def test_a_single_conversation_can_be_targeted(monkeypatch):
    from yoyo import core
    from yoyo.rag import ingest as ingest_mod

    first = core.new_conversation()
    core.persist_turn(first, "the first question, long enough", "an answer")
    second = core.new_conversation()
    core.persist_turn(second, "the second question, long enough", "an answer")

    seen = []
    monkeypatch.setattr(ingest_mod, "ingest_text",
                        lambda **kw: seen.append(kw["source_path"]) or True)
    sources.remember(conversation_ids=[second])
    assert seen == [f"conversation://{second}"]


def test_an_empty_conversation_is_not_ingested(monkeypatch):
    from yoyo import core
    from yoyo.rag import ingest as ingest_mod

    core.new_conversation()
    monkeypatch.setattr(ingest_mod, "ingest_text", lambda **kw: True)
    assert sources.remember().conversations == 0


def test_min_turns_filters_thin_conversations(monkeypatch):
    from yoyo import core
    from yoyo.rag import ingest as ingest_mod

    cid = core.new_conversation()
    core.persist_turn(cid, "one question long enough to count", "one answer long enough")
    monkeypatch.setattr(ingest_mod, "ingest_text", lambda **kw: True)
    assert sources.remember(min_turns=5).conversations == 0


def test_nothing_here_summarises_or_extracts():
    """Structural. The moment this module calls a model, the raw-source layer stops being
    raw, and every provenance check downstream is checking against generated text."""
    import inspect

    source = inspect.getsource(sources)
    for banned in ("llm.chat", "structured.generate", "summarize", "extract("):
        assert banned not in source, f"the raw-source layer calls {banned}"
