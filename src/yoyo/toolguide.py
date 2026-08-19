"""What each tool is for, in language that is not a function signature.

A tool's `description` is written for the model: terse, imperative, tuned to make it call the
right thing. It is not an explanation for a person. "Return the current reading from the
calibration sensor" tells you what the function does and nothing about when you would want
it, what it costs, or what it will refuse.

So this is a curated layer on top of the live registry. Two rules keep it honest:

**It never invents a tool.** The registry is the source of truth for what exists; this only
adds prose to names that are really mounted. A guide that lists a tool you do not have is
worse than no guide — it is the doc-drift problem this project already fails a test over.

**A tool with no entry is shown as UNDOCUMENTED, not hidden.** The whole reason this file
exists is that "34 tools" appeared in the corner with no way to find out which 34; silently
omitting the ones nobody has written copy for would recreate exactly that gap, one level
down. A test asserts every built-in has an entry.

Groups are by *what you are trying to do*, not by which server provides it. You do not think
"I need the vault MCP server", you think "what did I write about this".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Entry:
    group: str
    when: str        # when you would reach for this
    example: str     # something you could actually say to Yoyo
    caveat: str = ""  # what it will refuse, or get wrong


GROUPS = [
    ("Your documents", "The corpus — anything you have ingested."),
    ("Your notes", "The Obsidian vault. Yoyo reads it and writes only to `yoyo-drafts/`."),
    ("Mail", "Read and draft. There is no send path anywhere in this system."),
    ("Calendar", "Read only, enforced at the token, not just in code."),
    ("Tasks", "Markdown checkboxes in your vault. Yoyo never ticks one."),
    ("The web", "The only thing here that sends data off this machine."),
    ("Files", "A third-party server, scoped read-only by allowlist."),
    ("Time", "Boring and load-bearing."),
]

#: tool name -> Entry. Keys match the registry exactly.
GUIDE: dict[str, Entry] = {
    # ------------------------------------------------------------ documents ---
    "search_corpus": Entry(
        group="Your documents",
        when="You want something from a document you have ingested, and you want the answer "
             "to cite which one.",
        example="What does the GB10 baseline say about concurrency scaling?",
        caveat="Only searches what you have INGESTED. A file sitting on disk that you never "
               "ran `yoyo ingest` on is invisible here, and the answer will honestly say it "
               "found nothing.",
    ),
    "read_chunk": Entry(
        group="Your documents",
        when="A citation like [12] looked interesting and you want the passage around it.",
        example="Show me chunk 12 in full.",
    ),
    "corpus_stats": Entry(
        group="Your documents",
        when="Checking whether an ingest actually worked, or why retrieval is finding nothing.",
        example="How many documents do I have indexed?",
        caveat="Exact counts, not estimates — this exists so the model stops guessing them.",
    ),
    # ---------------------------------------------------------------- notes ---
    "vault_search": Entry(
        group="Your notes",
        when="You wrote something down and cannot remember where.",
        example="What did I note about the Lisbon trip?",
        caveat="Cannot see `yoyo-drafts/` or `yoyo-memory/`. Both are Yoyo's own output, and "
               "letting it search its own writing is how an assistant ends up citing itself.",
    ),
    "vault_read": Entry(
        group="Your notes",
        when="You know the note and want the whole thing rather than a matching line.",
        example="Read my MyAIServer note.",
    ),
    "vault_list": Entry(
        group="Your notes",
        when="Getting your bearings in a folder you have not opened for a while.",
        example="What notes do I have under Projects?",
    ),
    "vault_write_draft": Entry(
        group="Your notes",
        when="You want Yoyo to write something down for you.",
        example="Draft a summary of this conversation as a note.",
        caveat="Always lands in `yoyo-drafts/`, never in your notes proper. Promotion is you "
               "moving the file — that IS the approval step.",
    ),
    "vault_backlinks": Entry(
        group="Your notes",
        when="Finding everything that points at one note.",
        example="What links to my GB10 note?",
    ),
    # ----------------------------------------------------------------- mail ---
    "mail_search": Entry(
        group="Mail",
        when="Finding a message by sender, subject or content.",
        example="Any unread mail from Priya about the invoice?",
        caveat="Gmail is connected; Microsoft 365 needs its Entra registration first.",
    ),
    "mail_read": Entry(
        group="Mail",
        when="Reading one message in full, with a `mail:<id>` citation you can click.",
        example="Read the invoice email from Tuesday.",
    ),
    "mail_draft": Entry(
        group="Mail",
        when="Yoyo writes the reply; you read it and press send yourself.",
        example="Draft a reply saying I'll review it on Thursday.",
        caveat="**There is no send path in this system.** Not disabled by config — absent, "
               "and a structural test asserts it stays absent.",
    ),
    # ------------------------------------------------------------- calendar ---
    "calendar_agenda": Entry(
        group="Calendar",
        when="What is on today, tomorrow or this week.",
        example="What's on my calendar tomorrow?",
        caveat="Read-only at the OAuth scope. A calendar has no inert draft state — a "
               "'tentative' event has already invited other people.",
    ),
    "calendar_search": Entry(
        group="Calendar",
        when="Finding a meeting by title or attendee.",
        example="When is the review meeting with Alice?",
    ),
    # ---------------------------------------------------------------- tasks ---
    "tasks_list": Entry(
        group="Tasks",
        when="What is outstanding, and what is overdue.",
        example="What tasks are due this week?",
        caveat="Reads `- [ ]` checkboxes from your notes. **Yoyo never ticks one** — a silent "
               "state change is not an approval.",
    ),
    "tasks_summary": Entry(
        group="Tasks",
        when="The shape of the backlog rather than the items.",
        example="How many open tasks do I have?",
    ),
    # ------------------------------------------------------------------ web ---
    "web_search": Entry(
        group="The web",
        when="Anything current, or anything not in your own material.",
        example="What's the latest LiteLLM release?",
        caveat="**This sends your query off the machine** — through your own SearXNG, which "
               "means no vendor holds a log tied to you, but the queries still leave. Every "
               "one is recorded in `yoyo web egress`.",
    ),
    "web_fetch": Entry(
        group="The web",
        when="Reading a page the search only summarised.",
        example="Read that release note page and tell me what changed.",
        caveat="Refuses private and loopback addresses — checked AFTER DNS resolution, "
               "because a public hostname can resolve to 127.0.0.1. Fetched pages are "
               "wrapped as untrusted input; instructions inside them are shown, not obeyed.",
    ),
    # ---------------------------------------------------------------- files ---
    "files_read_file": Entry(
        group="Files",
        when="Reading a file that is not in the corpus or the vault.",
        example="Read the config file in my Notes folder.",
        caveat="Third-party server, scoped to an allowlist and read-only. Its write tools "
               "ship in the box and are withheld before the model ever sees them.",
    ),
    "files_list_directory": Entry(
        group="Files",
        when="Seeing what is in a folder.",
        example="What files are in my Notes folder?",
    ),
    # ----------------------------------------------------------------- time ---
    "current_time": Entry(
        group="Time",
        when="Anything relative — today, tomorrow, this week, overdue.",
        example="What's on today?",
        caveat="Boring and load-bearing: without it the model reasons about 'today' from its "
               "training cutoff and is confidently months out.",
    ),
}


def entry_for(name: str) -> Entry | None:
    """Exact match, then the un-prefixed name.

    MCP tools arrive prefixed with their server (`vault_search`, `files_read_file`), and the
    prefix is config — renaming a server in `yoyo-mcp.yaml` should not silently empty the
    guide.
    """
    if name in GUIDE:
        return GUIDE[name]
    if "_" in name:
        _, _, rest = name.partition("_")
        for key, entry in GUIDE.items():
            if key.partition("_")[2] == rest:
                return entry
    return None


def describe(name: str) -> dict[str, Any]:
    entry = entry_for(name)
    if entry is None:
        return {"group": "Not yet documented", "when": "", "example": "", "caveat": "",
                "documented": False}
    return {"group": entry.group, "when": entry.when, "example": entry.example,
            "caveat": entry.caveat, "documented": True}


def group_order() -> list[dict[str, str]]:
    return [{"name": name, "blurb": blurb} for name, blurb in GROUPS] + [
        {"name": "Not yet documented",
         "blurb": "Mounted and callable, but nobody has written the guide entry. Shown "
                  "rather than hidden — an undocumented tool you cannot see is the gap this "
                  "page exists to close."}
    ]
