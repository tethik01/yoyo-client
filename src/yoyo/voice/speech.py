"""Reshaping a written answer into one that survives being read aloud.

A spoken answer is not a written answer with TTS applied. Everything that makes Yoyo's
written answers trustworthy — `[12]`, `[mail:198a…]`, `[[MyAIServer]]`, full URLs, code
blocks, nested bullets — is unlistenable, and worse than unlistenable: a screen reader
saying "bracket twelve close bracket" trains the listener to tune out the exact tokens that
carry the provenance. Reading a 900-word answer aloud is four minutes with no way to skim.

So speech gets its own shape, built on two rules:

1. **Mechanical, never a second model call.** It is tempting to ask a model to "say this
   shorter". That is a fresh opportunity to fabricate, on text whose citations have already
   been stripped — there would be nothing left to check the rewrite against. Everything here
   is regex and sentence splitting: it can only ever *delete*, never invent.
2. **Nothing is silently dropped.** Citations are counted and announced ("four sources on
   screen"), code becomes "there's a code block on screen", truncation says so. The listener
   always knows the written answer has more, because the written answer stays the source of
   truth — voice is a view onto it, not a replacement for it.
"""

from __future__ import annotations

import re

#: Roughly 45 seconds at a normal TTS rate. Past this a listener has stopped following and
#: cannot rewind, which is the real constraint — not the engine's input limit.
DEFAULT_MAX_CHARS = 700

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
#: Corpus chunk `[12]`, mail `[mail:...]`, vault note `[Note.md]` — the four citation
#: vocabularies, minus markdown links (handled separately so the label survives).
_CITATION = re.compile(r"\[((?:mail:)?[A-Za-z0-9][^\]\s][^\]]{0,120})\](?!\()")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_BARE_URL = re.compile(r"(?<![(\w])https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RULE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,})\s*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_SPACES = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{2,}")


def plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def for_speech(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The written answer, as something worth listening to.

    Order matters: links before citations (a markdown link is also bracketed), code blocks
    before inline code, structure before truncation — truncating first would cut mid-list
    and then announce a source count for material that is no longer being read.
    """
    body = text or ""
    if not body.strip():
        return ""

    notes: list[str] = []

    body, code_blocks = _strip_code(body)
    if code_blocks:
        notes.append(f"There's {plural(code_blocks, 'a code block', 'code')} on screen.")

    body = _MD_LINK.sub(r"\1", body)
    body = _WIKILINK.sub(r"\1", body)

    links = len(_BARE_URL.findall(body))
    body = _BARE_URL.sub("", body)

    citations = len(_CITATION.findall(body))
    body = _CITATION.sub("", body)

    sources = citations + links
    if sources:
        notes.append(f"{sources} {plural(sources, 'source is', 'sources are')} on screen.")

    body = _tidy(_flatten(body))
    body, truncated = _shorten(body, max_chars)
    if truncated:
        notes.insert(0, "The rest is on screen.")

    return " ".join(part for part in [body.strip(), *notes] if part).strip()


def _strip_code(text: str) -> tuple[str, int]:
    blocks = len(_CODE_BLOCK.findall(text))
    text = _CODE_BLOCK.sub(" ", text)
    return _INLINE_CODE.sub(r"\1", text), blocks


def _flatten(text: str) -> str:
    """Markdown structure into sentences.

    Bullets become sentences rather than a numbered readout: "first, second, third" is how a
    person recites a list, and a TTS engine reading "dash" for forty lines is the single
    fastest way to make voice unusable.
    """
    text = _TABLE_ROW.sub(" ", text)
    text = _RULE.sub(" ", text)
    text = _HEADING.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _BULLET.sub("", text)
    text = _SPACES.sub(" ", text)

    lines = [line.strip() for line in text.splitlines()]
    parts: list[str] = []
    for line in lines:
        if not line:
            continue
        # A fragment without terminal punctuation runs into the next one when spoken.
        parts.append(line if line[-1] in ".!?:;," else line + ".")
    joined = " ".join(parts)
    return _BLANKS.sub(" ", _SPACES.sub(" ", joined)).strip()


def _tidy(text: str) -> str:
    """Clean up the holes removal leaves behind.

    Deleting `[mail:2]` from "it is unpaid [mail:2]." leaves "it is unpaid .", and a TTS
    engine handed a floating full stop pauses in the wrong place — the listener hears a
    sentence break that is not there.
    """
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"([.,;:!?])\1+", r"\1", text)
    return _SPACES.sub(" ", text).strip()


def _shorten(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut at a sentence boundary, never mid-clause.

    A hard character cut produces "the invoice from Priya was sent on the" and stops, which
    sounds like a crash. Losing the last sentence is better than sounding broken — and the
    caller announces that something was lost."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    kept: list[str] = []
    total = 0
    for sentence in _SENTENCE_END.split(text):
        if total + len(sentence) > max_chars:
            break
        kept.append(sentence)
        total += len(sentence) + 1
    if not kept:
        # One sentence longer than the whole budget — a wall of text with no punctuation,
        # which happens. Clip at a word boundary rather than keeping the whole thing: the
        # budget is the listener's attention, and honouring it only for well-punctuated
        # answers is honouring it never.
        clipped = text[:max_chars].rsplit(" ", 1)[0]
        return clipped.rstrip(" ,;:") + ".", True
    return " ".join(kept).strip(), True


def route_announcement(mode: str) -> str:
    """What the router chose, said out loud — not the reason, just the choice.

    Spoken automation hides its own decisions worse than written automation does: there is
    no chip on screen to glance at. Saying it costs a second and is the only reason routing
    is defensible with no visible UI. The *reason* stays written — a spoken justification
    for a choice the listener cannot see is noise before every single answer.
    """
    return {
        "ask": "Answering from your notes.",
        "agent": "Looking that up.",
        "plan": "This has a few parts — working through them.",
    }.get(mode, f"Using {mode}.")
