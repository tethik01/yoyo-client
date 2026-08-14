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
