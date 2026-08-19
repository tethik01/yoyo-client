"""Continuous memory: the sweep, the watermark, the cap, and the opt-out.

The pipeline was never the hard part — every piece already existed. What these tests hold is
the set of properties that make running it *continuously* safe rather than merely possible:

* the watermark makes a sweep incremental, and never loses a slice when extraction fails;
* the queue cap stops proposing before the review becomes a treadmill, and pauses rather
  than dropping work;
* a conversation can be told to be left alone, and that is not the same as forgetting;
* the sweep proposes and never writes — automation must not reach past the human judgement
  the whole review exists to preserve.
"""

from __future__ import annotations

import pytest

from yoyo.memory import pipeline, review
from yoyo.memory.wiki import Claim


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    monkeypatch.setattr(db_mod, "DEFAULT_PATH", path, raising=False)
    real = db_mod.connection
    monkeypatch.setattr(db_mod, "connection", lambda p=None: real(p or path))
    db_mod.migrate(path)
    return path


def conversation(turns: list[tuple[str, str]], idle_minutes: int = 60) -> int:
    """A conversation whose last activity was `idle_minutes` ago."""
    from yoyo.storage import db as db_mod

    with db_mod.connection() as conn, db_mod.transaction(conn):
        cur = conn.execute(
            "INSERT INTO conversations (title, updated_at) "
            "VALUES ('t', datetime('now', ?))", (f"-{idle_minutes} minutes",)
        )
        cid = int(cur.lastrowid)
        for role, content in turns:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                (cid, role, content),
            )
    return cid


def extraction(monkeypatch, per_call: list[list[dict]] | None = None):
    """Scripted extraction. Each call returns the next list, then repeats the last."""
    calls = {"n": 0, "texts": []}
    scripts = per_call or [[{"subject": "Priya", "kind": "person",
                             "claim": "is my sister", "quote": "my sister Priya"}]]

    def fake(source_id, text, role="extract"):
        calls["texts"].append(text)
        script = scripts[min(calls["n"], len(scripts) - 1)]
        calls["n"] += 1
        return [Claim(source=source_id, **c) for c in script]

    monkeypatch.setattr("yoyo.memory.extract.from_source", fake)
    return calls


# ------------------------------------------------------------------ candidates ---


def test_a_conversation_with_new_owner_turns_is_a_candidate():
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    assert len(pipeline.candidates()) == 1


def test_a_conversation_still_in_progress_is_left_alone():
    """A thread you are still typing in is not finished enough to summarise, and sweeping it
    every few minutes would propose its own intermediate states as facts."""
    conversation([("user", "My sister Priya is flying to Lisbon.")], idle_minutes=1)
    assert pipeline.candidates(idle_minutes=10) == []


def test_a_conversation_with_only_assistant_turns_is_not_a_candidate():
    """Yoyo's own words are never evidence — sweeping a monologue would be work with no
    possible output."""
    conversation([("assistant", "Here is everything I know about the GB10 box.")])
    assert pipeline.candidates() == []


def test_an_ignored_conversation_is_never_swept():
    cid = conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    pipeline.set_remember(cid, False)
    assert pipeline.candidates() == []


def test_ignoring_is_reversible():
    cid = conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    pipeline.set_remember(cid, False)
    pipeline.set_remember(cid, True)
    assert len(pipeline.candidates()) == 1


def test_conversations_are_remembered_by_default():
    """Opt-out, not opt-in. A memory you have to remember to enable stays empty."""
    cid = conversation([("user", "anything at all, said by me")])
    assert pipeline.is_remembered(cid)


# ------------------------------------------------------------------ watermarks ---


def test_a_sweep_advances_the_watermark_and_does_not_repeat_itself(monkeypatch):
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    calls = extraction(monkeypatch)

    first = pipeline.sweep()
    assert first.swept == 1 and first.queued == 1

    second = pipeline.sweep()
    assert second.considered == 0, "nothing new to read"
    assert calls["n"] == 1, "the model must not be called again for settled turns"


def test_only_the_new_slice_is_sent_to_the_model(monkeypatch):
    """The whole point of the watermark. Re-reading history every run costs more every day
    and nearly all of it is redoing settled work."""
    cid = conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    calls = extraction(monkeypatch)
    pipeline.sweep()

    from yoyo.storage import db as db_mod
    with db_mod.connection() as conn, db_mod.transaction(conn):
        conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                     (cid, "user", "Also, Priya works at Northwind Logistics."))
        conn.execute("UPDATE conversations SET updated_at = datetime('now', '-60 minutes') "
                     "WHERE id = ?", (cid,))

    pipeline.sweep()
    assert calls["n"] == 2
    assert "Northwind" in calls["texts"][1]
    assert "flying to Lisbon" not in calls["texts"][1], "old turns were re-sent"


def test_a_failed_extraction_does_not_advance_the_watermark(monkeypatch):
    """A lost slice is a memory that silently never existed. A repeated slice is a duplicate
    proposal, which the fingerprint already collapses — so failure must re-read, not skip."""
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])

    def boom(*a, **k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr("yoyo.memory.extract.from_source", boom)
    report = pipeline.sweep()
    assert report.failures
    assert pipeline.watermark("conversation://1") == 0

    extraction(monkeypatch)
    assert pipeline.sweep().queued == 1, "the slice must be re-read once the model is back"


def test_resetting_watermarks_re_reads_but_does_not_revive_rejections(monkeypatch):
    """After an extraction-prompt change the old prompt's reading is not evidence about the
    new one — but the owner's rejections are HIS memory, not the sweep's."""
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    extraction(monkeypatch)
    pipeline.sweep()
    review.decide(review.pending()[0]["id"], "rejected")

    pipeline.reset_watermarks()
    again = pipeline.sweep()
    assert again.swept == 1
    assert again.queued == 0, "a rejected claim must never be re-proposed"


# ------------------------------------------------------------------- the cap ---


def test_the_sweep_stops_at_the_queue_cap(monkeypatch):
    """The property that keeps review from becoming a rubber stamp."""
    for i in range(5):
        conversation([("user", f"My friend S{i} is coming on the trip in March.")])
    extraction(monkeypatch, [[{"subject": f"S{i}", "kind": "person", "claim": "coming along",
                               "quote": f"My friend S{i} is coming"}] for i in range(5)])

    cfg = pipeline.MemoryConfig(queue_cap=2)
    report = pipeline.sweep(cfg)
    assert report.capped
    assert report.pending_after <= 3, report.pending_after
    assert report.skipped_capped >= 1


def test_a_capped_sweep_loses_nothing(monkeypatch):
    """It pauses rather than skipping: the watermark for an unread conversation does not
    move, so clearing the queue resumes exactly where it stopped."""
    for i in range(3):
        conversation([("user", f"My friend S{i} is coming on the trip in March.")])
    extraction(monkeypatch, [[{"subject": f"S{i}", "kind": "person", "claim": "coming along",
                               "quote": f"My friend S{i} is coming"}] for i in range(3)])

    pipeline.sweep(pipeline.MemoryConfig(queue_cap=1))
    for row in review.pending():
        review.decide(row["id"], "rejected")

    after = pipeline.sweep(pipeline.MemoryConfig(queue_cap=10))
    assert after.swept >= 1, "the skipped conversations must still be waiting"


def test_a_disabled_config_sweeps_nothing(monkeypatch):
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    calls = extraction(monkeypatch)
    report = pipeline.sweep(pipeline.MemoryConfig(enabled=False))
    assert report.swept == 0 and calls["n"] == 0


def test_a_cap_of_zero_is_rejected_at_load(tmp_path):
    """Zero would disable review silently rather than visibly. `enabled: false` is the way
    to turn it off, and it says so."""
    path = tmp_path / "yoyo-memory.yaml"
    path.write_text("memory:\n  queue_cap: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="queue_cap"):
        pipeline.load_config(path)


# ------------------------------------------------------- the sweep never writes ---


def test_the_sweep_writes_nothing_to_the_vault(monkeypatch, tmp_path):
    """Automation must not reach past the judgement the review exists to preserve."""
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    extraction(monkeypatch)
    monkeypatch.setattr("yoyo.vault.vault_root", lambda: tmp_path)

    pipeline.sweep()
    assert list(tmp_path.rglob("*.md")) == []
    assert review.stats()["pending"] == 1


def test_the_sweep_has_no_path_to_apply():
    """Structural, not behavioural: a future edit that adds a write here fails this test
    rather than silently making the review decorative."""
    import inspect

    source = inspect.getsource(pipeline.sweep)
    for writer in ("apply_approved", "write_page", "write_pages", "write_index"):
        assert writer not in source


# --------------------------------------------------------------------- status ---


def test_status_reports_what_is_waiting(monkeypatch):
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    st = pipeline.status()
    assert st["waiting"] == 1
    assert st["conversations_swept"] == 0
    assert st["config"]["enabled"] is True


# ------------------------------------------------------------------ scheduler ---
# The clock is tested by passing it a time, not by waiting. A scheduler test that sleeps is
# a slow test that still cannot prove the interesting cases.


@pytest.fixture()
def clock(monkeypatch):
    from yoyo.scheduler import Scheduler

    queued = []
    monkeypatch.setattr("yoyo.jobs.recent", lambda **k: [])
    monkeypatch.setattr("yoyo.jobs.create", lambda kind, args=None: (queued.append((kind, args)), 1)[1])
    monkeypatch.setattr("yoyo.jobs.start", lambda job_id: None)
    return Scheduler(), queued


def test_an_idle_conversation_queues_a_sweep(clock):
    sched, queued = clock
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    assert sched.tick() == "a conversation went idle"
    assert queued[0][0] == "memory-sweep"


def test_nothing_to_read_queues_nothing(clock):
    sched, queued = clock
    assert sched.tick() is None
    assert queued == []


def test_the_nightly_pass_fires_once_per_day(clock):
    """The catch-up for evenings the laptop was shut. Once — a tick every 30 seconds would
    otherwise queue 120 sweeps in the 3am hour."""
    from datetime import datetime

    sched, queued = clock
    three_am = datetime(2026, 8, 15, 3, 0)
    assert sched.tick(three_am) == "nightly catch-up"
    assert sched.tick(datetime(2026, 8, 15, 3, 30)) is None
    assert sched.tick(datetime(2026, 8, 16, 3, 0)) == "nightly catch-up"
    assert len(queued) == 2


def test_a_disabled_config_stops_the_clock(clock, tmp_path, monkeypatch):
    sched, queued = clock
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])
    monkeypatch.setattr(pipeline, "load_config",
                        lambda path=None: pipeline.MemoryConfig(enabled=False))
    assert sched.tick() is None
    assert queued == []


def test_a_sweep_already_running_is_not_stacked(monkeypatch):
    """The runner serialises them anyway, but a queue full of identical pending sweeps makes
    the Jobs list useless for seeing what is actually happening."""
    from yoyo.scheduler import Scheduler

    created = []
    monkeypatch.setattr("yoyo.jobs.recent",
                        lambda **k: [{"status": "running", "kind": "memory-sweep"}])
    monkeypatch.setattr("yoyo.jobs.create", lambda kind, args=None: created.append(kind))
    monkeypatch.setattr("yoyo.jobs.start", lambda job_id: None)
    conversation([("user", "My sister Priya is flying to Lisbon on the 14th.")])

    assert Scheduler().tick() == "a conversation went idle"
    assert created == [], "a second sweep must not be queued while one is running"


def test_a_failing_tick_does_not_kill_the_loop(monkeypatch):
    """A scheduler that dies on one bad tick stops silently, which is the worst way for
    background work to fail."""
    from yoyo.scheduler import Scheduler

    sched = Scheduler()

    def boom():
        raise RuntimeError("database is locked")

    sched._safely(boom, "tick")   # must not raise
