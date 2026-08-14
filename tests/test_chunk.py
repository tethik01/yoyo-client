from yoyo.rag.chunk import chunk_text, estimate_tokens


def test_empty_input():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk():
    chunks = chunk_text("hello world", size=1200, overlap=150)
    assert len(chunks) == 1
    assert chunks[0].text == "hello world"
    assert chunks[0].ordinal == 0


def test_covers_all_content():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(30))
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 1
    joined = " ".join(c.text for c in chunks)
    for i in range(30):
        assert f"Paragraph {i}." in joined


def test_ordinals_are_contiguous():
    text = "sentence. " * 500
    chunks = chunk_text(text, size=300, overlap=30)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_respects_size_bound():
    text = "abc " * 2000
    chunks = chunk_text(text, size=400, overlap=40)
    assert all(len(c.text) <= 400 for c in chunks)


def test_no_boundary_text_still_terminates():
    text = "x" * 5000  # no whitespace anywhere
    chunks = chunk_text(text, size=250, overlap=25)
    assert len(chunks) > 1
    assert sum(len(c.text) for c in chunks) >= 5000


def test_overlap_validation():
    import pytest

    with pytest.raises(ValueError):
        chunk_text("abc", size=100, overlap=100)
    with pytest.raises(ValueError):
        chunk_text("abc", size=0)


def test_token_estimate_is_positive():
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * 400) == 100
