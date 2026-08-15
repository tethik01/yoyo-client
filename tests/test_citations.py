"""Gates for the constructed-citation scrubber.

Written against a real observed answer, not an imagined one: `coder` returned
`[MyAIServer.md](file:///Users/robertovivar/Dropbox/ObsidianVault/MyAIServer.md)` after a
tool that returns bare filenames. The username belongs to nobody on this machine.
"""

from __future__ import annotations

from yoyo.citations import fabricated_links, strip_fabricated_links

OBSERVED = (
    "Your vault mentions the GB10 box only in "
    "[MyAIServer.md](file:///Users/robertovivar/Dropbox/ObsidianVault/MyAIServer.md), "
    "describing it as an ASUS Ascent GX10."
)


def test_observed_answer_is_cleaned_and_keeps_the_real_identifier() -> None:
    cleaned, removed = strip_fabricated_links(OBSERVED)
    assert "file:///" not in cleaned
    assert "robertovivar" not in cleaned
    assert "[MyAIServer.md]" in cleaned          # the citation a tool actually supports
    assert "ASUS Ascent GX10" in cleaned         # substance survives
    assert removed == ["file:///"]


def test_clean_text_is_returned_unchanged_and_reports_nothing() -> None:
    good = "The box is described in [MyAIServer.md] and in chunk [7]."
    cleaned, removed = strip_fabricated_links(good)
    assert cleaned == good
    assert removed == []


def test_bare_path_in_prose_is_replaced_visibly_not_deleted() -> None:
    cleaned, removed = strip_fabricated_links("See C:\\Users\\bob\\notes\\a.md for detail.")
    assert "C:\\Users" not in cleaned
    assert "removed" in cleaned.lower()  # the user can see something was taken out
    assert removed


def test_windows_and_posix_home_paths_are_both_caught() -> None:
    for path in ("/home/claude/x.md", "/Users/bob/x.md", "C:\\Users\\bob\\x.md", "file:///x"):
        assert fabricated_links(f"see {path} ok"), path


def test_empty_and_none_are_safe() -> None:
    assert strip_fabricated_links("") == ("", [])
    assert strip_fabricated_links(None) == ("", [])  # type: ignore[arg-type]


def test_relative_paths_are_not_touched() -> None:
    """Tools DO return things like `notes/MyAIServer.md`. Scrubbing those would destroy
    real citations, which is a worse failure than leaving an invented one."""
    text = "See notes/MyAIServer.md and docs/model-baseline-gb10.md."
    cleaned, removed = strip_fabricated_links(text)
    assert cleaned == text
    assert removed == []


def test_evals_reexports_the_same_detector() -> None:
    """Two copies of this regex would drift, and the gate would stop matching the scrubber."""
    from yoyo import evals

    assert evals.fabricated_links is fabricated_links


# ------------------------------------------- invented web URLs (2026-08-15) ----
# Seen in the UI in `ask` mode, which has no tools at all: asked for local news, the model
# answered with three markdown links to news sites. It had made zero web requests. The
# domains may or may not exist — that is the point; they were plausible and clickable.
#
# The path-based detector above could not catch this, and was complete until today: before
# web search existed no tool could return an http URL, so one in an answer was just prose.
# Now that tools return URLs, the rule is expressible: a URL may appear only if a tool put
# it there. Provenance, not a blocklist — no opinion about which domains are real.

from yoyo.citations import strip_unsupported_urls, unsupported_urls, urls_in


def test_a_url_no_tool_returned_is_flagged():
    text = "Try [Mississauga Star](https://www.mississaugastar.ca/) for local news."
    assert unsupported_urls(text, sources="") == ["https://www.mississaugastar.ca/"]


def test_a_url_a_tool_returned_is_left_alone():
    sources = '{"url": "https://www.cbc.ca/news/canada/toronto", "title": "CBC"}'
    text = "CBC covered it: https://www.cbc.ca/news/canada/toronto"
    assert unsupported_urls(text, sources) == []


def test_case_and_trailing_slash_differences_are_not_treated_as_invention():
    """A model quoting `https://Example.com/A/` from a result containing
    `https://example.com/a` has copied it. Flagging that trains the owner to ignore the
    warning, which costs more than the false positive saves."""
    assert unsupported_urls("see https://Example.com/A/", '"https://example.com/a"') == []


def test_a_url_ending_a_sentence_keeps_its_full_stop_out_of_the_match():
    assert urls_in("go to https://example.com/a.") == ["https://example.com/a"]


def test_stripping_keeps_the_markdown_label_so_the_sentence_reads():
    text = "Try [Mississauga Star](https://www.mississaugastar.ca/) for news."
    cleaned, removed = strip_unsupported_urls(text, sources="")
    assert cleaned == "Try Mississauga Star for news."
    assert removed == ["https://www.mississaugastar.ca/"]


def test_a_bare_invented_url_is_replaced_visibly():
    cleaned, removed = strip_unsupported_urls("See https://made-up.example/x now", "")
    assert "made-up.example" not in cleaned
    assert "no tool returned it" in cleaned
    assert removed


def test_supported_and_invented_urls_in_one_answer_are_separated():
    sources = '"https://real.com/a"'
    text = "Real: https://real.com/a — Fake: https://fake.example/b"
    cleaned, removed = strip_unsupported_urls(text, sources)
    assert "https://real.com/a" in cleaned
    assert "fake.example" not in cleaned
    assert removed == ["https://fake.example/b"]


def test_text_with_no_urls_is_untouched():
    text = "The GB10 has 128 GB at 273 GB/s [7]."
    assert strip_unsupported_urls(text, "") == (text, [])


def test_empty_input_is_safe():
    assert strip_unsupported_urls("", "") == ("", [])
    assert unsupported_urls(None, None) == []


def test_the_ui_does_not_turn_a_markdown_link_into_a_citation_chip():
    """`[Label](url)` is a link, not a citation. Rendering its label as a clickable chip
    sent the user to a resolver that could only 404."""
    from pathlib import Path as _P

    import yoyo

    html = (_P(yoyo.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "(?!\\()" in html, "the citation pattern no longer excludes markdown links"
