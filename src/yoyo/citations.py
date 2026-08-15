"""Detecting and removing citations the model constructed rather than copied.

Observed twice on `coder`: a tool returns the identifier ``MyAIServer.md`` and the answer
comes back as ``[MyAIServer.md](file:///Users/robertovivar/Dropbox/ObsidianVault/MyAIServer.md)``
— a username belonging to nobody on this machine, a directory that does not exist, and a
link the user could click. Prompting against it (agent.py SYSTEM_PROMPT, three graph
prompts) reduced the rate but did not eliminate it, so there is a mechanical pass too.

The rule this encodes: an identifier is legitimate only if a tool returned it. Yoyo's tools
return bare names (``MyAIServer.md``) and integer chunk ids — never absolute paths, never
URLs. So any absolute-looking path in an answer was invented, and can be removed without
losing information: ``[MyAIServer.md](file:///...)`` degrades to ``[MyAIServer.md]``, which
is the citation the tool actually supports.

Stripping rather than failing is deliberate for the interactive path. The eval gate still
FAILS on a fabricated link — a model that needs the scrubber is a model with a defect, and
hiding that at gate time would be the same mistake as trusting the answer.
"""

from __future__ import annotations

import re

#: Absolute paths and file URLs. None of Yoyo's tools can return one of these, so a match
#: is always constructed text.
FABRICATED_LINK = re.compile(r"(file:///|[A-Za-z]:\\Users\\|/Users/|/home/[a-z]+/)")

#: A markdown link whose target is one of the above. Captures the label so it survives.
_MD_LINK = re.compile(
    r"\[([^\]\n]{1,200})\]\(\s*(?:file:///|[A-Za-z]:\\Users\\|/Users/|/home/[a-z]+/)[^)\n]*\)"
)

#: A bare fabricated path sitting in prose, not wrapped in a link.
_BARE = re.compile(
    r"(?:file:///|[A-Za-z]:\\Users\\[^\s)\]]*|/Users/[^\s)\]]*|/home/[a-z]+/[^\s)\]]*)"
    r"[^\s)\]]*"
)


def fabricated_links(text: str) -> list[str]:
    """The fabricated fragments present in `text`, deduplicated and sorted."""
    return sorted({m.group(0) for m in FABRICATED_LINK.finditer(text or "")})


def strip_fabricated_links(text: str) -> tuple[str, list[str]]:
    """Return `(cleaned, removed)`.

    Markdown links keep their label and lose the invented target. Bare paths are replaced
    with a visible marker rather than deleted silently — the user should be able to see
    that something was removed, otherwise the scrubber quietly launders a defect.
    """
    if not text:
        return text or "", []
    removed = fabricated_links(text)
    if not removed:
        return text, []
    cleaned = _MD_LINK.sub(lambda m: f"[{m.group(1)}]", text)
    cleaned = _BARE.sub("[path removed — not returned by any tool]", cleaned)
    return cleaned, removed


# ------------------------------------------------------- unsupported web URLs ---
#
# Observed live 2026-08-15, in `ask` mode with no web tool available: asked for local news,
# the model answered with three markdown links — mississaugastar.ca, a CBC section URL, the
# city's site. It had made a web request of exactly zero. The domains may or may not exist;
# that is the point. They were plausible, clickable, and invented.
#
# `FABRICATED_LINK` above did not catch them because it only knows about invented *file*
# paths. That was complete until today: before web search existed, no tool could return an
# http URL, so an http URL in an answer was unremarkable prose. Now tools DO return URLs,
# which makes the rule expressible for the first time:
#
#     a URL may appear in an answer only if a tool put it there.
#
# This is provenance, not a blocklist. It needs no opinion about which domains are real.

_URL = re.compile(r'https?://[^\s)\]}>\'"]+')

#: Trailing punctuation swept up by the pattern when a URL ends a sentence.
_TRAILING = ".,;:!?'\"`"


def _normalise(url: str) -> str:
    return url.rstrip(_TRAILING).rstrip("/").lower()


def urls_in(text: str) -> list[str]:
    return [m.group(0).rstrip(_TRAILING) for m in _URL.finditer(text or "")]


def unsupported_urls(text: str, sources: str) -> list[str]:
    """URLs in `text` that do not appear anywhere in `sources`.

    `sources` is every tool result of the turn, concatenated — cheap and blunt on purpose.
    A substring check over the raw payloads has no opinion about JSON shape, so a tool added
    later needs no registration here to be trusted.

    Comparison ignores case, a trailing slash and trailing punctuation: a model quoting
    `https://Example.com/A.` from a result containing `https://example.com/a` has copied it,
    not invented it, and flagging that would train the owner to ignore the warning.
    """
    if not text:
        return []
    haystack = (sources or "").lower()
    seen: dict[str, str] = {}
    for url in urls_in(text):
        key = _normalise(url)
        if key and key not in haystack and _normalise(url) not in haystack:
            seen.setdefault(key, url)
    return sorted(seen.values())


def strip_unsupported_urls(text: str, sources: str) -> tuple[str, list[str]]:
    """Remove URLs no tool produced. Markdown links keep their label.

    Removed rather than annotated because the failure mode is that they look checkable. A
    reader who clicks an invented link and lands on a parked domain has been misled twice.
    """
    invented = unsupported_urls(text, sources)
    if not invented:
        return text, []
    cleaned = text
    for url in invented:
        # `[Label](url)` -> `Label`, so the sentence still reads.
        cleaned = re.sub(
            r"\[([^\]\n]{1,200})\]\(\s*" + re.escape(url) + r"[^)\n]*\)",
            r"\1",
            cleaned,
        )
        cleaned = cleaned.replace(url, "[link removed — no tool returned it]")
    return cleaned, invented
