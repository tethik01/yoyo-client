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
