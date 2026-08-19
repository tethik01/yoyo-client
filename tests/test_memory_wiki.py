"""The wiki layer — Phases 2-4.

The owner chose automatic writing over a review queue. That is only defensible because of
one rule, and most of these tests exist to hold it:

    a claim traces to a RAW SOURCE, never to another wiki page.

Without it, a fact extracted wrongly on Monday is a retrievable source on Tuesday, cited on
Wednesday, and by Friday indistinguishable from something the owner said. The verbatim-quote
gate and the source-kind gate are the two mechanical checks that close that path, and neither
involves judgement.
"""

from __future__ import annotations

from yoyo.memory import build, wiki
from yoyo.memory.wiki import Claim

SOURCE = "conversation://14"
TEXT = (
    "# Conversation 14\n\n## Bhavin\n\nMy sister Priya is moving to Lisbon in March.\n\n"
    "## Yoyo\n\nNoted.\n"
)


def _claim(**kw) -> Claim:
    base = dict(subject="Priya", kind="person",
                claim="Priya is Bhavin's sister", quote="My sister Priya", source=SOURCE)
    base.update(kw)
    return Claim(**base)


# ------------------------------------------------- the rule the design rests on ---


def test_a_claim_citing_a_wiki_page_is_rejected():
    """THE laundering path. A model quoting a model, dressed as evidence."""
    result = wiki.verify([_claim(source="yoyo-memory/people/Priya.md")], {SOURCE: TEXT})
    assert result.accepted == []
    assert "never cite a wiki page" in result.rejected[0][1]


def test_a_claim_whose_quote_is_not_in_the_source_is_rejected():
    """The model cannot fake having read something — same trick as the golden eval's
    unguessable secret."""
    result = wiki.verify([_claim(quote="Priya works at NVIDIA")], {SOURCE: TEXT})
    assert result.accepted == []
    assert "does not appear" in result.rejected[0][1]


def test_a_claim_with_no_quote_is_rejected():
    assert wiki.verify([_claim(quote="  ")], {SOURCE: TEXT}).accepted == []


def test_a_claim_naming_a_source_that_was_not_supplied_is_rejected():
    """Unprovable is the same as fabricated for a system that must not launder its own
    output."""
    result = wiki.verify([_claim(source="conversation://999")], {SOURCE: TEXT})
    assert "was not supplied" in result.rejected[0][1]


def test_a_genuine_claim_is_accepted():
    result = wiki.verify([_claim()], {SOURCE: TEXT})
    assert len(result.accepted) == 1 and result.rejected == []


def test_quote_matching_tolerates_whitespace_and_smart_quotes():
    """Models reflow whitespace and swap glyphs when copying. Byte-identity would reject
    honest quotes and train us to loosen the gate, which is worse."""
    text = "She said “my sister  Priya” yesterday."
    result = wiki.verify([_claim(quote='"my sister Priya"')], {SOURCE: text})
    assert len(result.accepted) == 1


def test_an_unknown_entity_kind_is_rejected():
    assert wiki.verify([_claim(kind="spaceship")], {SOURCE: TEXT}).accepted == []


def test_extraction_never_lets_the_model_name_its_own_source():
    """A model that could name its source could name a wiki page as one. The source is set
    by the caller, and the schema has no field for it."""
    from yoyo.memory.extract import ClaimOut

    assert "source" not in ClaimOut.model_fields


def test_the_extraction_prompt_demands_a_verbatim_quote():
    from yoyo.memory import extract as extract_mod

    assert "EXACTLY" in extract_mod.INSTRUCTION
    assert "DISCARDED" in extract_mod.INSTRUCTION
    assert "EMPTY list" in extract_mod.INSTRUCTION


# ------------------------------------------------------------------- rendering ---


def test_a_page_declares_its_raw_sources():
    page = wiki.group([_claim()])[0]
    rendered = page.render(now="2026-08-15T12:00:00+00:00")
    assert "about: Priya" in rendered
    assert "derived_from:" in rendered
    assert f"  - {SOURCE}" in rendered
    assert "generated_by: yoyo" in rendered


def test_every_claim_on_a_page_shows_its_quote():
    """The page is the audit trail. A claim without its evidence visible is a claim you
    would have to trust."""
    rendered = wiki.group([_claim()])[0].render()
    assert "My sister Priya" in rendered
    assert f"`{SOURCE}`" in rendered


def test_pages_group_by_subject_and_kind():
    pages = wiki.group([
        _claim(),
        _claim(claim="Priya is moving in March", quote="moving to Lisbon in March"),
        _claim(subject="Lisbon", kind="place", claim="Lisbon is in Portugal",
               quote="moving to Lisbon"),
    ])
    assert {(p.subject, len(p.claims)) for p in pages} == {("Priya", 2), ("Lisbon", 1)}


def test_entities_from_one_source_are_linked():
    """Weak but honest: co-occurrence asserts they are related, not HOW — which is the part
    a model would have to guess."""
    pages = {p.subject: p for p in wiki.group([
        _claim(), _claim(subject="Lisbon", kind="place", quote="Lisbon"),
    ])}
    assert "Lisbon" in pages["Priya"].links


def test_filenames_survive_windows():
    assert wiki.safe_name('Bob "The Boss" <work>') == "Bob The Boss work"
    assert wiki.safe_name("   ") == "unnamed"
    assert wiki.safe_name("a/b\\c:d") == "abcd"


def test_the_index_groups_by_kind():
    rendered = wiki.render_index([
        {"subject": "Priya", "kind": "person", "claims": 2},
        {"subject": "Lisbon", "kind": "place", "claims": 1},
    ])
    assert "## Persons" in rendered and "## Places" in rendered
    assert "[[Priya]]" in rendered


def test_the_log_is_append_only(tmp_path):
    wiki.append_log(tmp_path, "build", {"accepted": 2})
    wiki.append_log(tmp_path, "forget", {"subject": "Priya"})
    text = (tmp_path / wiki.WIKI_DIR / wiki.LOG_FILE).read_text(encoding="utf-8")
    assert "**build**" in text and "**forget**" in text
    assert text.index("**build**") < text.index("**forget**")


# ------------------------------------------------------- identity (Phase 3) ------


def test_a_relational_word_next_to_a_name_raises_a_question_not_a_guess():
    """Merging two people makes a page wrong about both; splitting one scatters their
    memory. Ask."""
    questions = build.find_ambiguities([
        _claim(subject="Mom"), _claim(subject="Priya"),
    ], known={})
    assert len(questions) == 1
    assert "Mom" in questions[0]["question"] and "Priya" in questions[0]["question"]


def test_an_answered_alias_is_not_asked_again():
    questions = build.find_ambiguities(
        [_claim(subject="Mom"), _claim(subject="Priya")], known={"mom": "priya"}
    )
    assert questions == []


def test_two_proper_names_do_not_raise_a_question():
    assert build.find_ambiguities(
        [_claim(subject="Priya"), _claim(subject="Alice")], known={}
    ) == []


# ---------------------------------------------------------- time (Phase 4) ------


def test_a_restated_fact_is_not_duplicated():
    merged, superseded = build.reconcile([_claim()], [_claim()])
    assert len(merged) == 1 and superseded == 0


def test_a_contradiction_keeps_both_and_is_flagged_not_resolved():
    """The pattern's own rule: contradictions are recorded as edges, not resolved. An
    earlier version struck through the older claim — which needed a string heuristic to
    decide which two claims were "the same fact", and any heuristic crude enough to catch
    "lives in Toronto"/"lives in Lisbon" also catches "is my sister"/"is moving in March"."""
    old = _claim(claim="Priya lives in Toronto")
    new = _claim(claim="Priya lives in Lisbon")
    merged, flagged = build.reconcile([old], [new])
    assert len(merged) == 2, "both claims must survive"
    assert flagged == 1
    assert all("superseded" not in c.claim for c in merged), "nothing is struck through"


def test_a_false_positive_costs_a_glance_not_a_fact():
    """Two true facts sharing a leading predicate get flagged. That is acceptable — the
    output is a question on the page, not a deletion."""
    merged, flagged = build.reconcile(
        [_claim(claim="Priya is Bhavin's sister")],
        [_claim(claim="Priya is moving in March", quote="moving to Lisbon in March")],
    )
    assert len(merged) == 2
    assert all("~~" not in c.claim for c in merged)


def test_an_unrelated_new_fact_is_simply_added():
    merged, flagged = build.reconcile(
        [_claim()], [_claim(claim="Priya speaks Portuguese", quote="Priya")]
    )
    assert len(merged) == 2 and flagged == 0


def test_the_page_surfaces_contradictions_for_the_reader():
    page = wiki.group([
        _claim(claim="Priya lives in Toronto"),
        _claim(claim="Priya lives in Lisbon"),
    ])[0]
    rendered = page.render()
    assert "Possible contradictions" in rendered
    assert "does not decide which is right" in rendered
    assert "Toronto" in rendered and "Lisbon" in rendered


# -------------------------------------------- the owner's notes (Phase 3) -------


def test_yoyo_writes_only_between_its_markers():
    existing = "# Priya\n\nMy own notes about my sister. Do not touch.\n"
    merged = build.merge_into_existing(existing, "- moved to Lisbon")
    assert "My own notes about my sister. Do not touch." in merged
    assert build.BLOCK_START in merged and build.BLOCK_END in merged


def test_a_second_write_replaces_only_the_block():
    once = build.merge_into_existing("# Priya\n\nMine.\n", "- fact one")
    twice = build.merge_into_existing(once, "- fact two")
    assert "Mine." in twice
    assert "fact one" not in twice
    assert "fact two" in twice
    assert twice.count(build.BLOCK_START) == 1


def test_an_owner_note_is_preferred_over_a_new_page(tmp_path):
    """The owner's explicit choice: enrich the note they already keep rather than create a
    rival page and two nodes per person in the map."""
    (tmp_path / "People").mkdir()
    mine = tmp_path / "People" / "Priya.md"
    mine.write_text("# Priya\n\nMy sister.\n", encoding="utf-8")

    page = wiki.group([_claim()])[0]
    written = build.write_page(tmp_path, page)
    assert written == mine
    assert "My sister." in mine.read_text(encoding="utf-8")
    assert not (tmp_path / wiki.WIKI_DIR / "persons").exists()


def test_yoyos_own_pages_are_not_mistaken_for_owner_notes(tmp_path):
    page = wiki.group([_claim()])[0]
    build.write_page(tmp_path, page)
    assert build.owner_note_for(tmp_path, page) is None


def test_a_page_is_created_when_the_owner_has_no_note(tmp_path):
    page = wiki.group([_claim()])[0]
    written = build.write_page(tmp_path, page)
    assert wiki.WIKI_DIR in written.parts
    assert "Priya is Bhavin's sister" in written.read_text(encoding="utf-8")


# ------------------------------------------------------------ round trip --------


def test_pages_can_be_read_back_so_a_build_updates_rather_than_replaces(tmp_path):
    build.write_page(tmp_path, wiki.group([_claim()])[0])
    loaded = build.load_pages(tmp_path)
    claims = loaded[("person", "priya")]
    assert len(claims) == 1
    assert claims[0].source == SOURCE
    assert claims[0].quote == "My sister Priya"


def test_forgetting_removes_the_page_and_logs_a_tombstone(tmp_path):
    build.write_page(tmp_path, wiki.group([_claim()])[0])
    assert build.forget(tmp_path, "Priya") == 1
    assert build.load_pages(tmp_path) == {}
    log_text = (tmp_path / wiki.WIKI_DIR / wiki.LOG_FILE).read_text(encoding="utf-8")
    assert "**forget**" in log_text and "Priya" in log_text


def test_the_tombstone_does_not_repeat_what_was_forgotten(tmp_path):
    """Forgetting must actually forget. The log records THAT something went, not what."""
    build.write_page(tmp_path, wiki.group([_claim()])[0])
    build.forget(tmp_path, "Priya")
    log_text = (tmp_path / wiki.WIKI_DIR / wiki.LOG_FILE).read_text(encoding="utf-8")
    assert "sister" not in log_text.lower()


def test_generated_non_pages_are_not_read_back_as_entities(tmp_path):
    """SCHEMA.md was being loaded as an entity called "schema". Excluding by filename alone
    breaks again the next time a non-page file lands in the folder, so a page must declare
    `about:` to count as one."""
    from yoyo.memory import schema

    schema.write(tmp_path)
    build.write_index(tmp_path)
    build.write_page(tmp_path, wiki.group([_claim()])[0])

    loaded = build.load_pages(tmp_path)
    assert set(loaded) == {("person", "priya")}


# ------------------------------------------------------------------ dry run ---
# The command that answers "is this extraction worth keeping" must not require writing
# pages about your family to your vault in order to answer it.


def _transcript(said: str) -> str:
    """A real rendered transcript. Passing bare text would not exercise the owner/assistant
    split, and that split is now load-bearing."""
    from yoyo.memory import sources as sources_mod

    return sources_mod.render(1, "t", [{"role": "user", "content": said}])[0]


def _fake_extraction(monkeypatch, claims):
    monkeypatch.setattr(
        "yoyo.memory.extract.from_source",
        lambda source_id, text, role="extract": [
            Claim(source=source_id, **c) for c in claims
        ],
    )


def test_a_dry_run_writes_nothing_at_all(tmp_path, monkeypatch):
    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person",
                                    "claim": "flying to Lisbon",
                                    "quote": "Priya is flying to Lisbon"}])
    sources = {"conversation://1": _transcript("Priya is flying to Lisbon on the 14th.")}
    report = build.build(sources, root=tmp_path, dry_run=True)

    assert report.accepted == 1
    assert list(tmp_path.rglob("*.md")) == []


def test_a_dry_run_leaves_the_append_only_log_untouched(tmp_path, monkeypatch):
    """The log's only value is being an accurate record. A build that never happened must
    not appear in it."""
    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person", "claim": "c",
                                    "quote": "Priya"}])
    build.build({"conversation://1": _transcript("Priya is my sister and lives nearby")},
                root=tmp_path, dry_run=True)
    assert not (tmp_path / wiki.WIKI_DIR / wiki.LOG_FILE).exists()


def test_a_dry_run_returns_the_pages_so_the_claims_can_be_read(tmp_path, monkeypatch):
    """Counts cannot answer 'is this worth keeping'. A claim is only judgeable next to its
    quote, so the report has to carry both."""
    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person",
                                    "claim": "flying to Lisbon",
                                    "quote": "Priya is flying to Lisbon"}])
    report = build.build({"conversation://1": _transcript("Priya is flying to Lisbon.")},
                         root=tmp_path, dry_run=True)
    page = report.pages[0]
    assert page.subject == "Priya"
    assert page.claims[0].quote == "Priya is flying to Lisbon"
    assert "DRY RUN" in report.summary()


def test_a_dry_run_does_not_disturb_pages_already_on_disk(tmp_path, monkeypatch):
    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person", "claim": "first",
                                    "quote": "Priya"}])
    build.build({"conversation://1": _transcript("Priya is my sister, she lives nearby")},
                root=tmp_path)
    page_path = next(p for p in tmp_path.rglob("*.md") if "Priya" in p.name)
    before = page_path.read_text(encoding="utf-8")

    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person", "claim": "second",
                                    "quote": "Priya"}])
    build.build({"conversation://2": _transcript("Priya is my sister, she lives nearby")},
                root=tmp_path, dry_run=True)
    assert page_path.read_text(encoding="utf-8") == before


def test_a_real_run_still_writes(tmp_path, monkeypatch):
    """The dry-run flag must not become the default by accident."""
    _fake_extraction(monkeypatch, [{"subject": "Priya", "kind": "person", "claim": "c",
                                    "quote": "Priya"}])
    build.build({"conversation://1": _transcript("Priya is my sister, she lives nearby")},
                root=tmp_path)
    assert (tmp_path / wiki.WIKI_DIR / wiki.LOG_FILE).exists()
    assert any("Priya" in p.name for p in tmp_path.rglob("*.md"))


# ------------------------------- the model may not be its own source (found live) ---
# 2026-08-15. A dry run over two real conversations produced six pages — World War I, the
# Abraham Accords, Gaza — with every quote verifying and nothing rejected. A perfect run by
# every number, and worthless: the claims were quotes of *Yoyo* explaining geopolitics,
# filed as things to remember about the owner's life.
#
# The gates were never breached. They were walked around: half of every conversation
# transcript is model output, and "a claim must quote a raw source" did not say whose words
# a raw source contains. These tests pin the tightened rule.


def test_the_assistants_half_of_a_transcript_is_not_evidence():
    from yoyo.memory import sources as sources_mod

    text = sources_mod.render(1, "geopolitics", [
        {"role": "user", "content": "explain the West Asia conflict to me in simple terms"},
        {"role": "assistant",
         "content": "The current conflict features extensive foreign backing of both sides."},
    ])[0]

    evidence = build.evidence_from("conversation://1", text)
    assert "explain the West Asia conflict" in evidence
    assert "foreign backing" not in evidence


def test_a_note_the_owner_wrote_is_evidence_in_full():
    """The distinction is authorship, not file type. A vault note is the owner's prose all
    the way through, so none of it is dropped."""
    note = "# Trip\n\nPriya is flying to Lisbon on the 14th.\n"
    assert build.evidence_from("vault://Trip.md", note) == note


def test_a_claim_quoting_the_assistant_cannot_survive_verification(tmp_path, monkeypatch):
    """End to end, in the exact shape observed: the model proposes a claim quoting its own
    earlier answer. It must be rejected, not filed."""
    from yoyo.memory import sources as sources_mod

    text = sources_mod.render(1, "geopolitics", [
        {"role": "user", "content": "tell me about the Abraham Accords please"},
        {"role": "assistant", "content": "Regional alliances like the Abraham Accords matter."},
    ])[0]
    _fake_extraction(monkeypatch, [{
        "subject": "Abraham Accords", "kind": "organisation",
        "claim": "The Abraham Accords are an example of regional alliances",
        "quote": "Regional alliances like the Abraham Accords",
    }])

    report = build.build({"conversation://1": text}, root=tmp_path, dry_run=True)
    assert report.accepted == 0
    assert report.rejected
    assert list(tmp_path.rglob("*.md")) == []


def test_a_claim_quoting_the_owner_still_passes(tmp_path, monkeypatch):
    """The fix must not close the door on real memories — under-extracting everything would
    pass the test above and be just as useless."""
    from yoyo.memory import sources as sources_mod

    text = sources_mod.render(2, "family", [
        {"role": "user", "content": "My sister Priya is flying to Lisbon on the 14th."},
        {"role": "assistant", "content": "Noted. Want a reminder before the 14th?"},
    ])[0]
    _fake_extraction(monkeypatch, [{
        "subject": "Priya", "kind": "person", "claim": "Priya is the owner's sister",
        "quote": "My sister Priya is flying to Lisbon",
    }])

    report = build.build({"conversation://2": text}, root=tmp_path, dry_run=True)
    assert report.accepted == 1


def test_a_transcript_with_no_owner_turns_yields_nothing(tmp_path, monkeypatch):
    """Under-extraction is the safe direction. A source Yoyo did all the talking in has
    nothing in it the owner ever confirmed."""
    from yoyo.memory import sources as sources_mod

    text = sources_mod.render(3, "monologue", [
        {"role": "assistant", "content": "Here is everything I know about the GB10 box."},
    ])[0]
    _fake_extraction(monkeypatch, [{"subject": "GB10", "kind": "project", "claim": "c",
                                    "quote": "everything I know about the GB10"}])
    assert build.build({"conversation://3": text}, root=tmp_path, dry_run=True).accepted == 0


# ------------------------------------------- the review queue (Phase 2, 2026-08-15) ---
# The gates prove a claim is TRACEABLE. Only the owner can say whether it is WORTH KEEPING —
# six pages of world history passed every gate on the first real run. These tests pin the
# properties that make a review survivable rather than a rubber stamp.

import pytest

from yoyo.memory import review


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from yoyo.storage import db as db_mod

    path = tmp_path / "yoyo.db"
    monkeypatch.setattr(db_mod, "DEFAULT_PATH", path, raising=False)
    real = db_mod.connection
    monkeypatch.setattr(db_mod, "connection", lambda p=None: real(p or path))
    db_mod.migrate(path)
    return path


def _queued(subject="Priya", claim="is my sister", quote="my sister Priya",
           source="conversation://1", kind="person"):
    return Claim(subject=subject, kind=kind, claim=claim, quote=quote, source=source)


def test_a_proposal_is_queued_not_written(store, tmp_path):
    review.propose([_queued()])
    assert len(review.pending()) == 1
    assert list(tmp_path.rglob("*.md")) == []


def test_proposing_twice_does_not_ask_twice(store):
    review.propose([_queued()])
    second = review.propose([_queued()])
    assert second.proposed == 0
    assert second.already_pending == 1
    assert len(review.pending()) == 1


def test_a_rejected_claim_is_never_re_proposed(store):
    """The property that makes review survivable. A queue that re-asks what you already
    rejected is a treadmill, a treadmill gets rubber-stamped, and a rubber-stamped review is
    worse than none — it launders the same output while looking careful."""
    review.propose([_queued()])
    assert review.decide(review.pending()[0]["id"], "rejected")

    again = review.propose([_queued()])
    assert again.proposed == 0
    assert again.already_decided == 1
    assert review.pending() == []


def test_the_same_fact_from_a_different_conversation_is_a_separate_proposal(store):
    """Corroboration is information. Collapsing two sources into one claim would hide that
    the owner said it twice."""
    review.propose([_queued(source="conversation://1")])
    review.propose([_queued(source="conversation://2")])
    assert len(review.pending()) == 2


def test_a_reworded_claim_from_the_same_quote_is_not_asked_again(store):
    """Normalised fingerprint. A model that re-words its own summary of the same evidence
    does not get a second bite."""
    review.propose([_queued(claim="is my sister")])
    again = review.propose([_queued(claim="Is  My   Sister")])
    assert again.proposed == 0


def test_deciding_the_same_proposal_twice_reports_no_change(store):
    review.propose([_queued()])
    pid = review.pending()[0]["id"]
    assert review.decide(pid, "approved") is True
    assert review.decide(pid, "rejected") is False, "a decision is not silently re-openable"


def test_a_whole_subject_can_be_rejected_at_once(store):
    """The judgement that was actually needed on the first real run: 'this entire page is
    world history, not me'."""
    review.propose([
        _queued(subject="World War I", kind="event", claim="a", quote="q1"),
        _queued(subject="World War I", kind="event", claim="b", quote="q2"),
        _queued(subject="Priya"),
    ])
    assert review.decide_all("World War I", "rejected") == 2
    assert [r["subject"] for r in review.pending()] == ["Priya"]


def test_only_approved_claims_reach_a_page(store, tmp_path, monkeypatch):
    review.propose([_queued(subject="Priya"), _queued(subject="Gaza", kind="place",
                                                    claim="a conflict", quote="Gaza")])
    for row in review.pending():
        review.decide(row["id"], "approved" if row["subject"] == "Priya" else "rejected")

    result = build.apply_approved(root=tmp_path)
    written = [p.name for p in tmp_path.rglob("*.md")]
    assert result["claims"] == 1
    assert any("Priya" in n for n in written)
    assert not any("Gaza" in n for n in written)


def test_applying_twice_does_not_write_the_same_claim_twice(store, tmp_path):
    review.propose([_queued()])
    review.decide(review.pending()[0]["id"], "approved")
    build.apply_approved(root=tmp_path)
    second = build.apply_approved(root=tmp_path)
    assert second["claims"] == 0


def test_nothing_approved_writes_nothing(store, tmp_path):
    review.propose([_queued()])
    assert build.apply_approved(root=tmp_path)["claims"] == 0
    assert not (tmp_path / wiki.WIKI_DIR).exists()


def test_the_approval_rate_is_reported(store):
    """The number worth watching. Near 100% means it is proposing the obvious; near 0% means
    noise. Neither is visible from 'how many memories do I have'."""
    review.propose([_queued(subject="A", quote="a"), _queued(subject="B", quote="b"),
                    _queued(subject="C", quote="c")])
    rows = review.pending()
    review.decide(rows[0]["id"], "approved")
    review.decide(rows[1]["id"], "rejected")
    stats = review.stats()
    assert stats["pending"] == 1
    assert stats["approval_rate"] == 0.5


def test_an_empty_queue_has_no_approval_rate_rather_than_zero(store):
    """Zero would read as 'everything was rejected'. Nothing decided is not the same as
    nothing kept — the same absence-vs-evidence distinction as everywhere else here."""
    assert review.stats()["approval_rate"] is None
