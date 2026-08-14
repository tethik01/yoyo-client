"""Structured-output tests.

Local models wrap JSON in prose and fences even when told not to, and thinking is on by
default. Parsing has to be defensive, and a near-miss should cost one round trip rather
than the whole turn.
"""

import pytest
from pydantic import BaseModel

from yoyo import structured


class Shape(BaseModel):
    name: str
    count: int = 0


def _replies(monkeypatch, texts):
    seen = []

    class R:
        def __init__(self, t):
            self.text = t

    def fake_chat(messages, role="answer", **kw):
        seen.append(list(messages))
        return R(texts[min(len(seen) - 1, len(texts) - 1)])

    monkeypatch.setattr(structured.llm, "chat", fake_chat)
    return seen


# ------------------------------------------------------- extraction ----


def test_bare_json():
    assert structured.extract_json('{"a": 1}') == '{"a": 1}'


def test_fenced_json():
    assert structured.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_unlabelled_fence():
    assert structured.extract_json('```\n{"a": 1}\n```') == '{"a": 1}'


def test_json_surrounded_by_prose():
    text = 'Sure! Here you go:\n{"a": 1}\nHope that helps.'
    assert structured.extract_json(text) == '{"a": 1}'


def test_nested_braces_are_balanced_correctly():
    text = 'prefix {"a": {"b": [1, 2]}, "c": 3} suffix'
    assert structured.extract_json(text) == '{"a": {"b": [1, 2]}, "c": 3}'


def test_braces_inside_strings_do_not_confuse_the_parser():
    text = '{"a": "a } brace", "b": 2}'
    assert structured.extract_json(text) == text


def test_escaped_quotes_inside_strings():
    text = '{"a": "he said \\"hi\\" }", "b": 1}'
    assert structured.extract_json(text) == text


def test_arrays_are_supported():
    assert structured.extract_json("noise [1, 2, 3] noise") == "[1, 2, 3]"


def test_no_json_is_a_clear_error():
    with pytest.raises(structured.StructuredError, match="no JSON"):
        structured.extract_json("I would rather not.")


def test_empty_response_is_a_clear_error():
    with pytest.raises(structured.StructuredError, match="empty"):
        structured.extract_json("")


def test_unbalanced_json_is_reported():
    with pytest.raises(structured.StructuredError, match="unbalanced"):
        structured.extract_json('{"a": {"b": 1}')


# --------------------------------------------------------- generate ----


def test_valid_first_try(monkeypatch):
    seen = _replies(monkeypatch, ['{"name": "x", "count": 2}'])
    out = structured.generate(Shape, "make one")
    assert out.name == "x" and out.count == 2
    assert len(seen) == 1


def test_prose_wrapped_output_still_validates(monkeypatch):
    _replies(monkeypatch, ['Thinking done.\n```json\n{"name": "y"}\n```'])
    assert structured.generate(Shape, "make one").name == "y"


def test_invalid_output_is_retried_with_the_error_fed_back(monkeypatch):
    seen = _replies(monkeypatch, ['{"count": "not a number"}', '{"name": "fixed"}'])
    out = structured.generate(Shape, "make one")
    assert out.name == "fixed"
    assert len(seen) == 2
    # The retry must tell the model what was wrong, or it will repeat itself.
    assert "did not validate" in seen[1][-1]["content"]


def test_gives_up_after_max_attempts(monkeypatch):
    seen = _replies(monkeypatch, ["not json at all"])
    with pytest.raises(structured.StructuredError, match="after 2 attempts"):
        structured.generate(Shape, "make one", max_attempts=2)
    assert len(seen) == 2


def test_schema_is_included_in_the_system_prompt(monkeypatch):
    seen = _replies(monkeypatch, ['{"name": "x"}'])
    structured.generate(Shape, "make one")
    assert "count" in seen[0][0]["content"]


def test_context_is_passed_separately_from_the_instruction(monkeypatch):
    seen = _replies(monkeypatch, ['{"name": "x"}'])
    structured.generate(Shape, "the ask", context="background material")
    user = seen[0][1]["content"]
    assert "background material" in user and "the ask" in user
