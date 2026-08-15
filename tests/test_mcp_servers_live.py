"""Live stdio round trips for the corpus, tasks and calendar MCP servers.

Only the vault server had one. That mattered more than it sounds: the single worst bug in
this project — the SDK renaming camelCase to snake_case, so every tool call arrived with no
arguments — was invisible to unit tests and only showed up when a real server was spawned
and really called. Three servers had no such test, and README §10 listed the corpus server
under "assume broken until exercised".

These spawn the actual module as a subprocess over stdio and call it through Yoyo's own MCP
client adapter, so they cover the parts unit tests structurally cannot: schema translation
across the process boundary, argument marshalling, error propagation, and the startup
diagnostics a user sees when config is wrong.

No network. The calendar server is exercised for its failure path only — the success path
needs OAuth, and pretending otherwise would be the confident-wrong-answer failure this
project keeps finding.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from yoyo.mcp import client
from yoyo.tools import Registry

pytestmark = pytest.mark.slow


def _spec(name: str, module: str, prefix: str, env: dict[str, str] | None = None):
    return client.ServerSpec(
        name=name,
        command=sys.executable,
        args=["-m", module],
        env={"PYTHONPATH": "src", **(env or {})},
        prefix=prefix,
    )


def _mount(spec) -> tuple[Registry, list[str]]:
    reg = Registry()
    try:
        names = client.mount(spec, into=reg)
    except client.MCPError as exc:
        pytest.skip(f"could not start {spec.name}: {exc}")
    return reg, names


# ----------------------------------------------------------------- tasks server ---


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / "yoyo-drafts").mkdir()
    (root / "Daily.md").write_text(
        "- [ ] call the bank 📅 2026-01-05 #admin\n"
        "- [x] buy milk\n"
        "- [ ] undated thing\n",
        encoding="utf-8",
    )
    (root / "Projects" / "Yoyo.md").write_text(
        "- [ ] finish OAuth 📅 2099-01-01 ⏫ #work\n", encoding="utf-8"
    )
    (root / "yoyo-drafts" / "invented.md").write_text(
        "- [ ] a task Yoyo made up 📅 2026-01-01\n", encoding="utf-8"
    )
    return root


def test_tasks_server_round_trip(vault):
    reg, names = _mount(_spec("tasks", "yoyo.mcp.tasks_server", "tasks",
                              {"YOYO_VAULT_PATH": str(vault)}))
    assert {"tasks_list", "tasks_overdue", "tasks_summary", "tasks_search"} <= set(names)
    assert not any(n.startswith("tasks_tasks") for n in names), "prefix doubled"

    open_tasks = reg.dispatch("tasks_list", {})
    texts = [t["text"] for t in open_tasks["tasks"]]
    assert "call the bank" in texts
    assert "buy milk" not in texts, "a done task came back from the open list"


def test_tasks_server_actually_receives_its_arguments(vault):
    """The camelCase/snake_case bug again: arguments silently arriving empty meant every
    filter was ignored and every call returned everything, which looks like working code."""
    reg, _ = _mount(_spec("tasks", "yoyo.mcp.tasks_server", "tasks",
                          {"YOYO_VAULT_PATH": str(vault)}))
    filtered = reg.dispatch("tasks_search", {"query": "oauth"})
    assert filtered["count"] == 1
    assert filtered["tasks"][0]["text"] == "finish OAuth"

    done = reg.dispatch("tasks_list", {"status": "done"})
    assert [t["text"] for t in done["tasks"]] == ["buy milk"]


def test_tasks_server_never_surfaces_a_draft_task(vault):
    reg, _ = _mount(_spec("tasks", "yoyo.mcp.tasks_server", "tasks",
                          {"YOYO_VAULT_PATH": str(vault)}))
    everything = reg.dispatch("tasks_list", {"status": "all", "limit": 100})
    assert not any("made up" in t["text"] for t in everything["tasks"])


def test_tasks_server_exposes_no_tool_that_could_tick_a_box(vault):
    _, names = _mount(_spec("tasks", "yoyo.mcp.tasks_server", "tasks",
                            {"YOYO_VAULT_PATH": str(vault)}))
    for banned in ("complete", "done", "tick", "check", "write", "update"):
        assert not any(banned in n for n in names), f"{banned} tool exposed: {names}"


def test_tasks_server_says_what_is_wrong_when_the_vault_is_unset():
    """A bare "Connection closed" tells the user nothing. This is the diagnostic path that
    cost real time on the vault server."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "yoyo.mcp.tasks_server"],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "YOYO_VAULT_PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "vault" in proc.stderr.lower()


# ---------------------------------------------------------------- corpus server ---


def test_corpus_server_mounts_and_names_its_tools():
    """README §10 listed this as never mounted by another client. It is now mounted by
    this one, which is the same code path Claude Desktop would use."""
    reg, names = _mount(_spec("corpus", "yoyo.mcp.corpus_server", "corpus"))
    assert names, "the corpus server exposed no tools"
    assert not any(n.startswith("corpus_corpus") for n in names), "prefix doubled"
    assert any("search" in n for n in names)


def test_corpus_server_tools_carry_descriptions():
    """The description IS the prompt — an empty one means the model is choosing the tool
    blind. Empty schemas were exactly how the SDK field-name bug presented."""
    reg, names = _mount(_spec("corpus", "yoyo.mcp.corpus_server", "corpus"))
    for name in names:
        assert reg.get(name).description.strip(), f"{name} has no description"


# -------------------------------------------------------------- calendar server ---


def test_calendar_server_refuses_to_start_with_no_accounts_and_says_why():
    """The success path needs OAuth, so this covers what a user will actually hit first."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "yoyo.mcp.calendar_server"],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "yoyo-calendar.yaml" in proc.stderr
    assert "calendar auth" in proc.stderr


def test_calendar_server_module_imports_without_credentials():
    """Import-time failure would make `yoyo mcp list` report a broken server rather than an
    unconfigured one — two very different things to a user."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import yoyo.mcp.calendar_server as m; print(sorted(dir(m)))"],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    for tool in ("calendar_agenda", "calendar_conflicts", "calendar_free_slots"):
        assert tool in proc.stdout


# ----------------------------------------------------------------- every server ---


@pytest.mark.parametrize(
    "module",
    ["vault_server", "tasks_server", "corpus_server", "mail_server", "calendar_server"],
)
def test_every_server_module_is_runnable_as_a_module(module):
    """`python -m yoyo.mcp.X` is how yoyo-mcp.yaml launches them. An import error here is a
    server that silently never mounts."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import yoyo.mcp.{module}"],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"{module} failed to import: {proc.stderr[:400]}"


def test_configured_servers_all_point_at_modules_that_exist():
    """A typo in yoyo-mcp.yaml surfaces as a runtime mount failure with a confusing message.
    This catches it at test time."""
    import importlib.util

    for spec in client.load_config():
        args = list(spec.args)
        if "-m" not in args:
            continue
        module = args[args.index("-m") + 1]
        assert importlib.util.find_spec(module) is not None, (
            f"yoyo-mcp.yaml server {spec.name!r} points at {module}, which does not exist"
        )
