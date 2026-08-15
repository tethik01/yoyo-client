"""Spoken-answer shaping.

Tested as behaviour, not prose: the invariants are that speech never *adds* words that were
not in the written answer, never reads citation syntax aloud, and never silently drops
material without saying it did.
"""

from __future__ import annotations

from yoyo.voice import speech


def test_citations_are_not_read_aloud_but_are_counted():
    spoken = speech.for_speech(
        "The endpoint serves three capabilities [12] and the vault agrees [MyAIServer.md]."
    )
    assert "[12]" not in spoken
    assert "MyAIServer.md" not in spoken
    assert "2 sources are on screen" in spoken


def test_mail_citations_are_stripped_too():
    spoken = speech.for_speech("Priya sent the invoice [mail:198abc].")
    assert "mail:" not in spoken
    assert "Priya sent the invoice" in spoken


def test_urls_become_a_count_not_a_recitation():
    spoken = speech.for_speech("See https://example.com/a/very/long/path for details.")
    assert "http" not in spoken
    assert "1 source is on screen" in spoken


def test_markdown_link_keeps_its_label():
    spoken = speech.for_speech("Read [the release notes](https://example.com/notes).")
    assert "the release notes" in spoken
    assert "http" not in spoken


def test_code_blocks_are_announced_not_spelled_out():
    spoken = speech.for_speech("Run this:\n\n```\nyoyo doctor --verbose\n```\n\nThen retry.")
    assert "yoyo doctor" not in spoken
    assert "code block on screen" in spoken
    assert "Then retry" in spoken


def test_bullets_become_sentences():
    spoken = speech.for_speech("Three checks:\n- corpus\n- vault\n- endpoint")
    assert "-" not in spoken
    assert "corpus" in spoken and "endpoint" in spoken


def test_headings_and_emphasis_lose_their_markers():
    spoken = speech.for_speech("## Summary\n\nThe **endpoint** is _up_.")
    assert "#" not in spoken and "*" not in spoken and "_" not in spoken
    assert "endpoint" in spoken


def test_long_answers_are_cut_at_a_sentence_and_say_so():
    long_text = " ".join(f"Sentence number {i} says something." for i in range(60))
    spoken = speech.for_speech(long_text, max_chars=200)
    assert "The rest is on screen" in spoken
    assert len(spoken) < len(long_text)
    # Never mid-clause: a hard cut sounds like a crash.
    body = spoken.split("The rest is on screen")[0].strip()
    assert body.endswith(".")


def test_a_single_enormous_sentence_still_terminates_cleanly():
    spoken = speech.for_speech("word " * 400, max_chars=100)
    assert spoken.endswith("on screen.")
    assert len(spoken) < 200


def test_short_answers_pass_through_essentially_unchanged():
    spoken = speech.for_speech("The endpoint is up.")
    assert spoken == "The endpoint is up."


def test_empty_in_empty_out():
    assert speech.for_speech("") == ""
    assert speech.for_speech("   \n  ") == ""


def test_speech_only_deletes_never_invents():
    """The reason this module is regex and not a model call: a rewrite would be a second
    chance to fabricate, on text whose citations have already been removed."""
    written = "Priya sent the invoice on Tuesday [mail:1] and it is unpaid [mail:2]."
    spoken = speech.for_speech(written)
    words = lambda text: {w.strip(".,;:!?") for w in text.lower().split()} - {""}  # noqa: E731
    added = words(spoken) - words(written)
    # Only the fixed announcement vocabulary may be new — no rewriting, no synonyms.
    assert added <= {"2", "sources", "source", "are", "is", "on", "screen", "rest", "the"}


def test_route_is_announced_in_speech_without_the_reason():
    for mode in ("ask", "agent", "plan"):
        line = speech.route_announcement(mode)
        assert line and line.endswith(".")
        assert "mode" not in line.lower()
