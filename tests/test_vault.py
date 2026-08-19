"""Vault tests.

Path confinement is the security boundary of this module — the vault is the user's real
notes, and a traversal bug means Yoyo can read or write anywhere on the disk. It is tested
from both directions: legitimate paths work, escapes fail.
"""

import pytest

from yoyo import vault


@pytest.fixture()
def v(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "Daily").mkdir()

    (root / "Projects" / "Yoyo.md").write_text(
        "---\ntags: [project]\n---\n\n# Yoyo\n\nA private assistant. See [[Hardware]].\n",
        encoding="utf-8",
    )
    (root / "Hardware.md").write_text(
        "# Hardware\n\nThe GB10 box runs the models.\n", encoding="utf-8"
    )
    (root / "Daily" / "2026-08-14.md").write_text(
        "Worked on [[Yoyo]] today. Espresso machine still broken.\n", encoding="utf-8"
    )
    (root / ".obsidian" / "config.md").write_text("should never appear", encoding="utf-8")
    (root / "notes.txt").write_text("not markdown", encoding="utf-8")

    monkeypatch.setenv("YOYO_VAULT_PATH", str(root))
    return root


# ---------------------------------------------------------- confinement ----


@pytest.mark.parametrize(
    "bad",
    ["../outside.md", "../../etc/passwd", "Projects/../../escape.md", "/etc/passwd"],
)
def test_paths_escaping_the_vault_are_refused(v, bad):
    with pytest.raises(vault.VaultError, match="escapes the vault"):
        vault._resolve(bad)


def test_legitimate_nested_path_resolves(v):
    assert vault._resolve("Projects/Yoyo.md").is_file()


def test_symlink_out_of_the_vault_is_refused(v, tmp_path):
    """resolve() before checking containment, so a symlink cannot smuggle a path through."""
    secret = tmp_path / "secret.md"
    secret.write_text("private", encoding="utf-8")
    link = v / "innocent.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(vault.VaultError, match="escapes the vault"):
        vault._resolve("innocent.md")


def test_missing_vault_config_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("YOYO_VAULT_PATH", raising=False)
    from yoyo.config import get_settings

    monkeypatch.setattr(get_settings(), "vault_path", None, raising=False)
    with pytest.raises(vault.VaultError, match="No vault configured"):
        vault.vault_root()


# ---------------------------------------------------------------- read ----


def test_list_notes_finds_markdown_and_skips_the_rest(v):
    paths = {n.path for n in vault.list_notes()}
    assert paths == {"Hardware.md", "Projects/Yoyo.md", "Daily/2026-08-14.md"}
    assert not any("obsidian" in p for p in paths)
    assert not any(p.endswith(".txt") for p in paths)


def test_list_notes_can_scope_to_a_folder(v):
    assert [n.path for n in vault.list_notes("Projects")] == ["Projects/Yoyo.md"]


def test_read_note_splits_frontmatter_and_extracts_links(v):
    note = vault.read_note("Projects/Yoyo.md")
    assert note["title"] == "Yoyo"
    assert "tags: [project]" in note["frontmatter"]
    assert not note["text"].startswith("---")
    assert note["links"] == ["Hardware"]


def test_read_missing_note_is_a_clear_error(v):
    with pytest.raises(vault.VaultError, match="No note at"):
        vault.read_note("Nope.md")


def test_search_is_case_insensitive_and_reports_the_line(v):
    hits = vault.search("gb10")
    assert len(hits) == 1
    assert hits[0].path == "Hardware.md"
    assert hits[0].line == 3
    assert "GB10" in hits[0].excerpt


def test_search_returns_one_hit_per_note(v):
    (v / "Repeats.md").write_text("yoyo\nyoyo\nyoyo\n", encoding="utf-8")
    assert sum(1 for h in vault.search("yoyo") if h.path == "Repeats.md") == 1


def test_empty_query_returns_nothing(v):
    assert vault.search("   ") == []


def test_backlinks_finds_referring_notes(v):
    assert vault.backlinks("Yoyo.md") == ["Daily/2026-08-14.md"]


def test_backlinks_excludes_the_note_itself(v):
    (v / "Self.md").write_text("I link to [[Self]].\n", encoding="utf-8")
    assert "Self.md" not in vault.backlinks("Self")


# --------------------------------------------------------------- write ----


def test_draft_lands_in_the_drafts_folder(v):
    r = vault.write_draft("summary", "# Summary\n")
    assert r["path"] == f"{vault.DRAFTS_DIR}/summary.md"
    assert (v / vault.DRAFTS_DIR / "summary.md").read_text(encoding="utf-8") == "# Summary\n"


def test_draft_name_cannot_escape_into_the_vault_proper(v):
    """The whole point: Yoyo must not be able to overwrite canon."""
    before = (v / "Hardware.md").read_text(encoding="utf-8")
    r = vault.write_draft("../Hardware.md", "OVERWRITTEN")
    assert r["path"].startswith(vault.DRAFTS_DIR)
    assert (v / "Hardware.md").read_text(encoding="utf-8") == before


def test_draft_name_with_nested_path_is_flattened(v):
    r = vault.write_draft("Projects/deep/thing.md", "x")
    assert r["path"] == f"{vault.DRAFTS_DIR}/thing.md"


def test_existing_draft_is_not_clobbered_silently(v):
    vault.write_draft("note", "first")
    with pytest.raises(vault.VaultError, match="already exists"):
        vault.write_draft("note", "second")
    assert (v / vault.DRAFTS_DIR / "note.md").read_text(encoding="utf-8") == "first"


def test_overwrite_is_possible_when_asked_for(v):
    vault.write_draft("note", "first")
    vault.write_draft("note", "second", overwrite=True)
    assert (v / vault.DRAFTS_DIR / "note.md").read_text(encoding="utf-8") == "second"


def test_extension_is_added_when_missing(v):
    assert vault.write_draft("plain", "x")["path"].endswith(".md")


def test_hidden_draft_names_are_rejected(v):
    with pytest.raises(vault.VaultError, match="Invalid draft name"):
        vault.write_draft(".hidden", "x")


def test_drafts_are_not_searchable_as_canon(v):
    """A draft is Yoyo's own unapproved output. If search returned it, the assistant could
    cite itself as a source."""
    vault.write_draft("invention", "The espresso warranty expires 2027.")
    assert vault.search("espresso warranty") == []
    assert all(vault.DRAFTS_DIR not in n.path for n in vault.list_notes())


def test_stats_counts_notes_and_drafts(v):
    vault.write_draft("d1", "x")
    s = vault.stats()
    assert s["notes"] == 3
    assert s["drafts"] == 1


@pytest.mark.parametrize("blank", ["", "   ", "."])
def test_blank_vault_path_is_treated_as_unset(monkeypatch, blank):
    """Path("") resolves to "." — an empty setting would silently make the working
    directory the vault, which is how Yoyo would end up reading the whole repo."""
    monkeypatch.setenv("YOYO_VAULT_PATH", blank)
    from yoyo.config import get_settings

    monkeypatch.setattr(get_settings(), "vault_path", None, raising=False)
    with pytest.raises(vault.VaultError, match="No vault configured"):
        vault.vault_root()


# ------------------------ memory pages: on the map, never in search (2026-08-15) ---
# The hole: extraction refused to read `yoyo-memory/` from day one, so a wiki page could
# never become evidence at WRITE time. Nothing stopped `vault_search` finding those pages at
# READ time and handing them to the model as "your notes" — the same circularity through the
# other door, and undetectable, because the quote really is in the file.


def _memory_vault(tmp_path):
    (tmp_path / "Trip.md").write_text("Lisbon in March.\n", encoding="utf-8")
    mem = tmp_path / vault.MEMORY_DIR / "people"
    mem.mkdir(parents=True)
    (mem / "Priya.md").write_text(
        "---\nabout: Priya\nkind: person\n---\n\nPriya is flying to Lisbon.\n",
        encoding="utf-8",
    )
    drafts = tmp_path / vault.DRAFTS_DIR
    drafts.mkdir()
    (drafts / "Draft.md").write_text("Lisbon draft\n", encoding="utf-8")
    return tmp_path


def test_search_cannot_see_yoyo_written_memory_pages(tmp_path, monkeypatch):
    root = _memory_vault(tmp_path)
    monkeypatch.setattr(vault, "vault_root", lambda: root)
    hits = vault.search("Lisbon")
    paths = [h.path for h in hits]
    assert any("Trip" in p for p in paths), "the owner's own note must still be findable"
    assert not any(vault.MEMORY_DIR in p for p in paths)
    assert not any(vault.DRAFTS_DIR in p for p in paths)


def test_listing_notes_excludes_memory_pages(tmp_path, monkeypatch):
    root = _memory_vault(tmp_path)
    monkeypatch.setattr(vault, "vault_root", lambda: root)
    assert [n.title for n in vault.list_notes()] == ["Trip"]


def test_the_map_still_shows_memory_pages_and_marks_them(tmp_path, monkeypatch):
    """Drawing a node is not citing it. The map is the whole reason the memory exists in a
    vault rather than a database, so excluding it there would defeat the point."""
    root = _memory_vault(tmp_path)
    monkeypatch.setattr(vault, "vault_root", lambda: root)
    g = vault.graph()
    by_title = {n["title"]: n for n in g["nodes"]}
    assert "Priya" in by_title
    assert by_title["Priya"]["generated"] is True
    assert by_title["Trip"]["generated"] is False
    assert g["stats"]["generated"] == 1


def test_drafts_stay_out_of_the_map_as_before(tmp_path, monkeypatch):
    """Memory pages are reviewed output; drafts are unreviewed. Only one of them earns a
    place on the map."""
    root = _memory_vault(tmp_path)
    monkeypatch.setattr(vault, "vault_root", lambda: root)
    assert "Draft" not in [n["title"] for n in vault.graph()["nodes"]]


def test_the_two_modules_agree_on_the_folder_name():
    """Two spellings of one folder name is how an exclusion silently stops excluding."""
    from yoyo.memory import wiki

    assert vault.MEMORY_DIR == wiki.WIKI_DIR


def test_stats_counts_memory_pages_separately(tmp_path, monkeypatch):
    root = _memory_vault(tmp_path)
    monkeypatch.setattr(vault, "vault_root", lambda: root)
    st = vault.stats()
    assert st["notes"] == 1, "Yoyo's pages are not notes the owner wrote"
    assert st["memory_pages"] == 1
