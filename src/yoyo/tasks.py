"""Tasks extracted from the Obsidian vault's checkbox syntax.

The vault is already canon. Tasks live in it as `- [ ] do the thing` scattered across daily
notes and project notes, which is a perfectly good place to keep them and a terrible place
to query them. This module reads that syntax and returns structured items the agent can
filter — no new store, no new credentials, no sync to keep honest.

**Read-only, and deliberately so.** Yoyo does not tick your boxes. The vault's write
asymmetry (drafts only) exists because approval is a human moving a file; marking a task
done is exactly the kind of silent state change that asymmetry is meant to prevent. A future
`tasks_propose_done` writing to a draft would respect it. Editing the note in place would not.

Dates are the hard part. There is no standard: Obsidian Tasks uses `📅 2026-08-20`,
Dataview uses `[due:: 2026-08-20]`, and most people just write `due 2026-08-20` or nothing
at all. All three are parsed, plus a bare ISO date. What is NOT done is guessing at
"tomorrow" or "next Friday" — a wrong due date is worse than no due date, because it silently
reorders what you think is urgent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

log = logging.getLogger(__name__)

#: `- [ ] text`, `* [x] text`, `2. [/] text`, with any indent. The status char is kept
#: verbatim rather than collapsed to done/not-done: Obsidian users overload it (`/` for in
#: progress, `-` for cancelled, `?` for question) and flattening that would lose real signal.
TASK_LINE = re.compile(
    r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+\[(?P<status>.)\]\s+(?P<text>.*\S)\s*$"
)

_ISO = r"(\d{4}-\d{2}-\d{2})"
#: Ordered by how explicit the marker is. An emoji or a `due::` field is an unambiguous
#: statement of intent; a bare date in the text is a guess, so it is tried last.
_DUE_PATTERNS = (
    re.compile(r"📅\s*" + _ISO),                        # Obsidian Tasks
    re.compile(r"\[\s*due\s*::\s*" + _ISO + r"\s*\]"),  # Dataview inline field
    re.compile(r"(?i)\bdue:?\s*" + _ISO),               # plain "due 2026-08-20"
    re.compile(r"\b" + _ISO + r"\b"),                   # bare date, last resort
)
_SCHEDULED = re.compile(r"⏳\s*" + _ISO)
_DONE_DATE = re.compile(r"✅\s*" + _ISO)
_PRIORITY = {"⏫": "high", "🔺": "highest", "🔼": "medium", "🔽": "low", "⏬": "lowest"}
_TAG = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]*)")

#: Characters that mean "this is no longer open". Everything else — including `/` for in
#: progress and `?` for a question — counts as open, because an unfinished task you have
#: started is still unfinished.
CLOSED_STATUSES = frozenset({"x", "X", "-", "~"})

#: Metadata that should not be read back as part of the task text.
_STRIP_FROM_TEXT = re.compile(
    r"(📅|⏳|✅)\s*\d{4}-\d{2}-\d{2}|\[\s*(due|scheduled|completion)\s*::[^\]]*\]|[⏫🔺🔼🔽⏬]"
)


@dataclass(slots=True)
class Task:
    text: str
    status: str
    note: str
    line: int
    due: date | None = None
    scheduled: date | None = None
    done_on: date | None = None
    priority: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def open(self) -> bool:
        return self.status not in CLOSED_STATUSES

    def overdue(self, today: date) -> bool:
        return bool(self.open and self.due and self.due < today)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "open": self.open,
            "note": self.note,
            "line": self.line,
            "due": self.due.isoformat() if self.due else None,
            "scheduled": self.scheduled.isoformat() if self.scheduled else None,
            "done_on": self.done_on.isoformat() if self.done_on else None,
            "priority": self.priority,
            "tags": list(self.tags),
        }


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        # A real date-shaped string that is not a real date (2026-13-45). Dropping it is
        # correct: a task with no due date sorts last, a task with a fabricated one lies.
        return None


def _first_date(text: str, patterns) -> date | None:  # noqa: ANN001
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            parsed = _parse_date(m.group(1))
            if parsed:
                return parsed
    return None


def parse_line(line: str, note: str, number: int) -> Task | None:
    """One line to a Task, or None if it is not a checkbox."""
    m = TASK_LINE.match(line)
    if not m:
        return None

    raw = m.group("text")
    done_on = _first_date(raw, (_DONE_DATE,))
    scheduled = _first_date(raw, (_SCHEDULED,))
    due = _first_date(raw, _DUE_PATTERNS)

    # A completion date is not a due date. Without this, `- [x] ship it ✅ 2026-08-14`
    # parses as due-on-the-day-it-was-finished via the bare-ISO fallback.
    if due and due == done_on and not any(p.search(raw) for p in _DUE_PATTERNS[:3]):
        due = None

    priority = next((label for glyph, label in _PRIORITY.items() if glyph in raw), None)
    tags = tuple(sorted({t for t in _TAG.findall(raw)}))

    text = _STRIP_FROM_TEXT.sub("", raw)
    # Tags are captured structurally above, so leaving them inline duplicates them — and
    # the model then repeats "#admin" back in prose. Stripped only when something readable
    # survives: "- [ ] #admin" is a task whose entire text is a tag, and blanking it would
    # turn a real item into an empty row.
    without_tags = re.sub(r"\s{2,}", " ", _TAG.sub("", text)).strip()
    text = without_tags if without_tags else re.sub(r"\s{2,}", " ", text).strip()

    return Task(
        text=text,
        status=m.group("status"),
        note=note,
        line=number,
        due=due,
        scheduled=scheduled,
        done_on=done_on,
        priority=priority,
        tags=tags,
    )


def parse_note(text: str, note: str) -> list[Task]:
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        task = parse_line(line, note, i)
        if task:
            out.append(task)
    return out


def collect(folder: str = "", limit_notes: int = 2000) -> list[Task]:
    """Every task in the vault. Drafts are excluded — `vault._notes` already does that, so
    Yoyo cannot surface a task it invented in its own draft as if it were yours."""
    from . import vault

    root = vault.vault_root()
    base = vault._resolve(folder, root) if folder else root
    if not base.is_dir():
        raise vault.VaultError(f"{folder!r} is not a folder in the vault")

    out: list[Task] = []
    for path in sorted(vault._notes(base))[:limit_notes]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("could not read %s: %s", path, exc)
            continue
        out.extend(parse_note(text, vault._rel(path, root)))
    return out


def _sort_key(task: Task) -> tuple:
    """Overdue and soonest first; undated last. An undated task sorting before a dated one
    would bury the thing with a deadline, which is the whole reason to have this list."""
    order = {"highest": 0, "high": 1, "medium": 2, None: 3, "low": 4, "lowest": 5}
    return (
        task.due is None,
        task.due or date.max,
        order.get(task.priority, 3),
        task.note,
        task.line,
    )


def query(
    status: str = "open",
    folder: str = "",
    due_before: str | None = None,
    tag: str | None = None,
    contains: str | None = None,
    limit: int = 100,
    today: date | None = None,
) -> list[Task]:
    """Filter the vault's tasks. `status` is open | done | all."""
    if status not in {"open", "done", "all"}:
        raise ValueError(f"status must be open, done or all — got {status!r}")

    cutoff = None
    if due_before:
        cutoff = _parse_date(due_before)
        if cutoff is None:
            raise ValueError(f"due_before must be YYYY-MM-DD — got {due_before!r}")

    tasks = collect(folder)
    if status == "open":
        tasks = [t for t in tasks if t.open]
    elif status == "done":
        tasks = [t for t in tasks if not t.open]

    if cutoff:
        # An undated task is not "due before" anything. Including it would pad every
        # deadline query with everything else you have ever written down.
        tasks = [t for t in tasks if t.due and t.due <= cutoff]
    if tag:
        wanted = tag.lstrip("#").lower()
        tasks = [t for t in tasks if any(x.lower() == wanted for x in t.tags)]
    if contains:
        needle = contains.lower()
        tasks = [t for t in tasks if needle in t.text.lower()]

    tasks.sort(key=_sort_key)
    return tasks[:limit]


def summary(today: date | None = None) -> dict[str, Any]:
    """Counts, for a "what's on my plate" turn that should not pull in 200 task lines."""
    now = today or date.today()
    tasks = collect()
    open_tasks = [t for t in tasks if t.open]
    overdue = [t for t in open_tasks if t.overdue(now)]
    dated = [t for t in open_tasks if t.due]
    return {
        "total": len(tasks),
        "open": len(open_tasks),
        "done": len(tasks) - len(open_tasks),
        "overdue": len(overdue),
        "due_today": sum(1 for t in dated if t.due == now),
        "undated_open": len(open_tasks) - len(dated),
        "notes_with_tasks": len({t.note for t in tasks}),
        "as_of": now.isoformat(),
    }
