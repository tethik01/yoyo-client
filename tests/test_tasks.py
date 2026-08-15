"""Task extraction from vault checkboxes.

Parsing is pure, so it is tested directly; the vault-walking parts get a real temp vault.
The date handling carries most of the risk here — a wrong due date silently reorders what
the user believes is urgent, which is worse than no due date at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from yoyo import tasks
from yoyo.tasks import parse_line, parse_note

# ------------------------------------------------------------------ syntax ---


@pytest.mark.parametrize(
    "line",
    [
        "- [ ] call the bank",
        "* [ ] call the bank",
        "+ [ ] call the bank",
        "1. [ ] call the bank",
        "2) [ ] call the bank",
        "    - [ ] call the bank",
        "\t- [ ] call the bank",
    ],
)
def test_every_checkbox_flavour_obsidian_accepts_is_parsed(line):
    task = parse_line(line, "n.md", 1)
    assert task is not None and task.text == "call the bank"


@pytest.mark.parametrize(
    "line",
    ["- not a task", "just text", "- [] missing space", "", "   ", "# [ ] a heading"],
)
def test_non_tasks_are_ignored(line):
    assert parse_line(line, "n.md", 1) is None


def test_open_and_done_are_distinguished():
    assert parse_line("- [ ] a", "n.md", 1).open is True
    assert parse_line("- [x] a", "n.md", 1).open is False
    assert parse_line("- [X] a", "n.md", 1).open is False


def test_in_progress_counts_as_open():
    """Obsidian users overload the status char. `/` means started, and a started task is
    still unfinished — flattening it to done would hide real work."""
    task = parse_line("- [/] half done", "n.md", 1)
    assert task.status == "/" and task.open is True


def test_cancelled_counts_as_closed():
    assert parse_line("- [-] abandoned", "n.md", 1).open is False


def test_status_character_is_preserved_verbatim():
    assert parse_line("- [?] is this needed", "n.md", 1).status == "?"


# ------------------------------------------------------------------- dates ---


def test_obsidian_tasks_emoji_due_date():
    assert parse_line("- [ ] file the return 📅 2026-09-01", "n.md", 1).due == date(2026, 9, 1)


def test_dataview_inline_field_due_date():
    assert parse_line("- [ ] pay it [due:: 2026-09-01]", "n.md", 1).due == date(2026, 9, 1)


def test_plain_english_due_date():
    assert parse_line("- [ ] renew due 2026-09-01", "n.md", 1).due == date(2026, 9, 1)
    assert parse_line("- [ ] renew due: 2026-09-01", "n.md", 1).due == date(2026, 9, 1)


def test_bare_iso_date_is_a_last_resort_but_is_used():
    assert parse_line("- [ ] meeting 2026-09-01", "n.md", 1).due == date(2026, 9, 1)


def test_no_date_means_no_date_not_a_guess():
    """Nothing here infers 'tomorrow' or 'next Friday'. A fabricated deadline reorders the
    user's priorities behind their back."""
    for line in ("- [ ] sometime soon", "- [ ] next week", "- [ ] urgent!!"):
        assert parse_line(line, "n.md", 1).due is None


def test_impossible_date_is_dropped_rather_than_crashing():
    assert parse_line("- [ ] x 2026-13-45", "n.md", 1).due is None


def test_completion_date_is_not_mistaken_for_a_due_date():
    """`- [x] ship it ✅ 2026-08-14` was previously parsed as due on the day it was
    finished, via the bare-ISO fallback."""
    task = parse_line("- [x] ship it ✅ 2026-08-14", "n.md", 1)
    assert task.done_on == date(2026, 8, 14)
    assert task.due is None


def test_an_explicit_due_date_survives_alongside_a_completion_date():
    task = parse_line("- [x] ship it 📅 2026-08-10 ✅ 2026-08-14", "n.md", 1)
    assert task.due == date(2026, 8, 10)
    assert task.done_on == date(2026, 8, 14)


def test_scheduled_is_separate_from_due():
    task = parse_line("- [ ] draft ⏳ 2026-08-20 📅 2026-08-25", "n.md", 1)
    assert task.scheduled == date(2026, 8, 20)
    assert task.due == date(2026, 8, 25)


# ------------------------------------------------------- metadata and text ---


def test_metadata_is_stripped_from_the_readable_text():
    task = parse_line("- [ ] file the return 📅 2026-09-01 ⏫ #admin", "n.md", 1)
    assert "2026-09-01" not in task.text
    assert "⏫" not in task.text
    assert task.text.startswith("file the return")


def test_priority_is_read():
    assert parse_line("- [ ] a ⏫", "n.md", 1).priority == "high"
    assert parse_line("- [ ] a 🔽", "n.md", 1).priority == "low"
    assert parse_line("- [ ] a", "n.md", 1).priority is None


def test_tags_are_collected_and_deduplicated():
    task = parse_line("- [ ] a #work #admin #work", "n.md", 1)
    assert task.tags == ("admin", "work")


def test_nested_tags_survive():
    assert parse_line("- [ ] a #work/yoyo", "n.md", 1).tags == ("work/yoyo",)


def test_line_numbers_are_one_based_so_they_match_an_editor():
    found = parse_note("intro\n- [ ] first\ntext\n- [ ] second\n", "n.md")
    assert [t.line for t in found] == [2, 4]


def test_overdue_needs_both_open_and_a_past_due_date():
    today = date(2026, 8, 14)
    assert parse_line("- [ ] a 📅 2026-08-01", "n.md", 1).overdue(today) is True
    assert parse_line("- [x] a 📅 2026-08-01", "n.md", 1).overdue(today) is False
    assert parse_line("- [ ] a 📅 2026-09-01", "n.md", 1).overdue(today) is False
    assert parse_line("- [ ] a", "n.md", 1).overdue(today) is False


# ------------------------------------------------------------- over a vault ---


@pytest.fixture()
def vault_with_tasks(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / "yoyo-drafts").mkdir()

    (root / "Daily.md").write_text(
        "# Daily\n"
        "- [ ] call the bank 📅 2026-08-01 #admin\n"
        "- [x] buy milk\n"
        "- [ ] write the report 📅 2026-12-01 #work\n"
        "- [ ] think about it\n",
        encoding="utf-8",
    )
    (root / "Projects" / "Yoyo.md").write_text(
        "- [ ] finish OAuth 📅 2026-08-20 ⏫ #work\n- [/] measure the graph\n",
        encoding="utf-8",
    )
    (root / "yoyo-drafts" / "invented.md").write_text(
        "- [ ] a task Yoyo made up 📅 2026-08-02\n", encoding="utf-8"
    )
    monkeypatch.setenv("YOYO_VAULT_PATH", str(root))
    return root


def test_tasks_are_collected_across_the_vault(vault_with_tasks):
    found = tasks.collect()
    assert len(found) == 6
    assert {t.note for t in found} == {"Daily.md", "Projects/Yoyo.md"}


def test_drafts_are_excluded_so_yoyo_cannot_surface_its_own_invention(vault_with_tasks):
    """Same rule as vault search: drafts are Yoyo's unapproved output, not the user's
    commitments. A task the assistant wrote appearing in 'what am I late on' would be a
    fabrication with a deadline attached."""
    assert all("yoyo-drafts" not in t.note for t in tasks.collect())
    assert not any("made up" in t.text for t in tasks.collect())


def test_open_is_the_default_filter(vault_with_tasks):
    assert all(t.open for t in tasks.query())


def test_done_filter_returns_only_closed(vault_with_tasks):
    done = tasks.query(status="done")
    assert [t.text for t in done] == ["buy milk"]


def test_sorted_soonest_first_with_undated_last(vault_with_tasks):
    got = tasks.query()
    assert got[0].text == "call the bank"       # earliest due
    assert got[-1].due is None                   # undated sinks


def test_due_before_excludes_undated_tasks(vault_with_tasks):
    """An undated task is not 'due before' anything. Including it would pad every deadline
    query with everything the user ever wrote down."""
    got = tasks.query(due_before="2026-08-21")
    assert {t.text for t in got} == {"call the bank", "finish OAuth"}


def test_tag_filter_is_case_insensitive_and_hash_tolerant(vault_with_tasks):
    assert len(tasks.query(tag="work")) == 2
    assert len(tasks.query(tag="#WORK")) == 2


def test_contains_filter_searches_task_text(vault_with_tasks):
    assert [t.text for t in tasks.query(contains="oauth")] == ["finish OAuth"]


def test_folder_filter_scopes_to_a_subtree(vault_with_tasks):
    assert all(t.note.startswith("Projects/") for t in tasks.query(folder="Projects"))


def test_bad_status_is_rejected(vault_with_tasks):
    with pytest.raises(ValueError, match="open, done or all"):
        tasks.query(status="pending")


def test_bad_due_before_is_rejected_rather_than_silently_ignored(vault_with_tasks):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tasks.query(due_before="next tuesday")


def test_limit_is_honoured(vault_with_tasks):
    assert len(tasks.query(limit=2)) == 2


def test_summary_counts_are_consistent(vault_with_tasks):
    s = tasks.summary(today=date(2026, 8, 14))
    assert s["total"] == 6
    assert s["open"] + s["done"] == s["total"]
    assert s["overdue"] == 1                    # "call the bank", due 2026-08-01
    assert s["undated_open"] == 2               # "think about it", "measure the graph"
    assert s["notes_with_tasks"] == 2


def test_summary_does_not_count_future_tasks_as_overdue(vault_with_tasks):
    assert tasks.summary(today=date(2026, 1, 1))["overdue"] == 0


def test_unreadable_note_does_not_kill_the_whole_collection(vault_with_tasks, monkeypatch):
    import pathlib

    original = pathlib.Path.read_text

    def explode(self, *a, **kw):
        if self.name == "Daily.md":
            raise OSError("locked by another process")
        return original(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", explode)
    found = tasks.collect()
    assert found and all(t.note == "Projects/Yoyo.md" for t in found)


# ------------------------------------------------------------------ policy ----


def test_there_is_no_way_to_mark_a_task_done():
    """Structural guard. Yoyo reads the vault and writes drafts only; ticking a box in
    place is exactly the silent state change that asymmetry exists to prevent."""
    import inspect

    from yoyo.mcp import tasks_server

    for module in (tasks, tasks_server):
        source = inspect.getsource(module)
        for banned in ("write_text", "def complete", "def mark_done", "def tick"):
            assert banned not in source, f"{module.__name__} contains {banned}"


def test_tags_are_removed_from_the_readable_text():
    """Captured in `tags`; leaving them inline makes the model echo "#admin" back in prose."""
    task = parse_line("- [ ] call the bank #admin", "n.md", 1)
    assert task.text == "call the bank"
    assert task.tags == ("admin",)


def test_a_task_that_is_only_a_tag_keeps_its_text():
    """Blanking it would turn a real item into an empty row in the list."""
    task = parse_line("- [ ] #admin", "n.md", 1)
    assert task.text == "#admin"
    assert task.tags == ("admin",)
