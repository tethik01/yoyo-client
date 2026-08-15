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
