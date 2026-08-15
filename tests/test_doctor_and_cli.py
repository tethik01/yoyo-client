"""Doctor's offline checks, and a smoke test over every CLI command.

Two gaps this closes.

`yoyo doctor` is the gate — README §5 says nothing below it is trustworthy until it is
green — and it had no tests at all. Its network checks cannot run here, but the ones that
matter most for silent wrongness can: the tool-fidelity check (which carried the same stale
"point them at `agent`" text that was wrong in two docs), and the vault check (an empty
`YOYO_VAULT_PATH` once made the working directory the vault, and pointing at `test-vault`
while believing it is the real one gives correct answers about the wrong corpus).

The CLI smoke test exists because `yoyo talk`, `yoyo voice status` and the calendar and
tasks groups were wired up and never invoked. A command that raises on `--help` is broken
for everyone, and that is cheap to check for all 36 at once.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from yoyo import doctor
from yoyo.cli import app

runner = CliRunner()


def _checks(monkeypatch, offline: bool = True) -> dict[str, doctor.Check]:
    """Run only the checks that work with no server, no Docker and no key."""
    if offline:
        monkeypatch.setattr(doctor, "_server", lambda checks: [])
        monkeypatch.setattr(doctor, "_roles", lambda checks, served: None)
        monkeypatch.setattr(doctor, "_embeddings", lambda checks: None)
        monkeypatch.setattr(doctor, "_sqlite", lambda checks: None)
        monkeypatch.setattr(doctor, "_qdrant", lambda checks: None)
    return {c.name: c for c in doctor.run_all()}


# ------------------------------------------------------------- tool fidelity ---


def test_tool_fidelity_passes_on_the_real_config(monkeypatch):
    got = _checks(monkeypatch)["tool fidelity"]
    assert got.ok, got.detail


def test_tool_fidelity_fails_when_a_tool_role_points_at_a_fabricating_endpoint(monkeypatch):
    from yoyo.config import Role

    class FakeModels:
        roles = {"bad": Role(name="bad", endpoint="fast", tools=True)}

    monkeypatch.setattr(doctor, "get_models", lambda: FakeModels())
    checks: list[doctor.Check] = []
    doctor._tool_fidelity(checks)
    assert checks[0].ok is False
    assert "bad -> fast" in checks[0].detail


def test_the_failure_message_no_longer_says_only_agent(monkeypatch):
    """It said "Point them at 'agent'" — untrue since ADR-027 promoted `coder`. Doctor is
    the first place a confused user looks, so a stale instruction here is expensive."""
    from yoyo.config import Role

    class FakeModels:
        roles = {"bad": Role(name="bad", endpoint="fast", tools=True)}

    monkeypatch.setattr(doctor, "get_models", lambda: FakeModels())
    checks: list[doctor.Check] = []
    doctor._tool_fidelity(checks)
    assert "coder" in checks[0].detail


# -------------------------------------------------------------------- vault ---


def test_an_unset_vault_is_reported_as_fine_not_broken(monkeypatch):
    """Vault features are optional. Failing doctor over an unconfigured vault would train
    the user to ignore a red check, which defeats the point of having one."""
    monkeypatch.delenv("YOYO_VAULT_PATH", raising=False)
    from yoyo import vault

    monkeypatch.setattr(vault, "vault_root", lambda: (_ for _ in ()).throw(
        vault.VaultError("No vault configured. Set YOYO_VAULT_PATH in .env ...")
    ))
    got = _checks(monkeypatch)["vault"]
    assert got.ok and "not configured" in got.detail


def test_a_vault_path_that_is_set_but_unusable_fails(monkeypatch, tmp_path):
    """Unset and misconfigured are different verdicts. The first check conflated them, so a
    YOYO_VAULT_PATH pointing at a file reported "not configured" — which reads as "you have
    not set this up" when in fact you have, wrongly."""
    file_not_folder = tmp_path / "notes.md"
    file_not_folder.write_text("x", encoding="utf-8")
    monkeypatch.setenv("YOYO_VAULT_PATH", str(file_not_folder))
    got = _checks(monkeypatch)["vault"]
    assert got.ok is False
    assert "set but unusable" in got.detail


def test_a_vault_path_that_no_longer_exists_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("YOYO_VAULT_PATH", str(tmp_path / "moved-away"))
    got = _checks(monkeypatch)["vault"]
    assert got.ok is False


def test_a_real_looking_vault_reports_its_note_count(monkeypatch, tmp_path):
    root = tmp_path / "MyVault"
    root.mkdir()
    for i in range(9):
        (root / f"n{i}.md").write_text(f"# {i}", encoding="utf-8")
    monkeypatch.setenv("YOYO_VAULT_PATH", str(root))
    got = _checks(monkeypatch)["vault"]
    assert got.ok and "9 notes" in got.detail
    assert "scaffold" not in got.detail


def test_the_test_scaffold_is_flagged_without_failing(monkeypatch, tmp_path):
    """Pointing at `test-vault` and believing it is the real vault produces answers that are
    correct about the wrong corpus — the hardest kind of wrong to notice."""
    root = tmp_path / "test-vault"
    root.mkdir()
    (root / "a.md").write_text("# a", encoding="utf-8")
    monkeypatch.setenv("YOYO_VAULT_PATH", str(root))
    got = _checks(monkeypatch)["vault"]
    assert got.ok, "a scaffold vault is a warning, not a failure"
    assert "scaffold" in got.detail


# --------------------------------------------------------- optional configs ---


def test_optional_configs_summarise_mail_calendar_and_voice(monkeypatch):
    got = _checks(monkeypatch)["optional configs"]
    assert got.ok, got.detail
    for word in ("mail", "calendar", "voice"):
        assert word in got.detail


def test_a_malformed_optional_config_is_reported_not_raised(monkeypatch):
    """`yoyo doctor` exists to explain breakage. Doctor itself crashing on a bad yaml is the
    worst possible behaviour for that job."""
    from yoyo import calendar as cal

    monkeypatch.setattr(cal, "load_accounts", lambda: (_ for _ in ()).throw(
        ValueError("provider must be 'google' or 'microsoft', got 'fastmail'")
    ))
    got = _checks(monkeypatch)["optional configs"]
    assert got.ok is False
    assert "fastmail" in got.detail


def test_doctor_makes_no_network_call_for_the_optional_checks(monkeypatch):
    """These must stay runnable on a train. If they ever start touching OAuth, doctor stops
    being the cheap first thing you run."""
    import httpx

    def explode(*a, **k):
        raise AssertionError("doctor's offline checks made a network call")

    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "post", explode)
    _checks(monkeypatch)


def test_summary_is_json_serialisable_for_the_health_endpoint(monkeypatch):
    import json

    monkeypatch.setattr(doctor, "run_all", lambda: [doctor.Check("x", True, "fine")])
    json.dumps(doctor.summary())


# ---------------------------------------------------------------------- CLI ---


def _all_commands() -> list[list[str]]:
    out: list[list[str]] = [[c.name or c.callback.__name__] for c in app.registered_commands]
    for group in app.registered_groups:
        out.append([group.name])
        for c in group.typer_instance.registered_commands:
            out.append([group.name, c.name or c.callback.__name__])
    return out


@pytest.mark.parametrize("argv", _all_commands(), ids=lambda a: " ".join(a))
def test_every_command_renders_its_help_without_raising(argv):
    """Catches import errors, bad option definitions and typos in the wiring — the whole
    class of "this command was never once invoked" bug."""
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0, f"`yoyo {' '.join(argv)} --help` failed:\n{result.output}"


def test_every_command_has_a_help_string():
    """`--help` output with a blank description is a command nobody can discover."""
    missing = []
    for c in app.registered_commands:
        if not (c.help or (c.callback.__doc__ or "").strip()):
            missing.append(c.name or c.callback.__name__)
    for group in app.registered_groups:
        for c in group.typer_instance.registered_commands:
            if not (c.help or (c.callback.__doc__ or "").strip()):
                missing.append(f"{group.name} {c.name or c.callback.__name__}")
    assert not missing, f"commands with no help text: {missing}"


def test_talk_rejects_an_unknown_mode_before_touching_the_microphone():
    result = runner.invoke(app, ["talk", "--mode", "nonsense"])
    assert result.exit_code != 0
    assert "ask, agent or plan" in result.output


def test_tasks_list_rejects_a_relative_due_date():
    """Consistent with the MCP tool: no "next tuesday". A misparsed deadline silently
    reorders what the user believes is urgent."""
    result = runner.invoke(app, ["tasks", "list", "--due-before", "next tuesday"])
    assert result.exit_code != 0


def test_calendar_agenda_rejects_a_malformed_day():
    result = runner.invoke(app, ["calendar", "agenda", "--day", "14-08-2026"])
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output


# ------------------------------------------ duplicate .env keys (2026-08-15) ---
# Found live: `.env` had two YOYO_VAULT_PATH lines after an edit — the old scaffold path and
# the new real one. dotenv takes the last, so it worked; but which value was in force was
# invisible to a reader, and the losing line looked just as authoritative. Same class as a
# doc disagreeing with the code, and the same fix: make it fail loudly.


def test_duplicate_env_keys_are_found(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "YOYO_LLM_API_KEY=a\n"
        "YOYO_VAULT_PATH=C:\\old\\test-vault\n"
        "YOYO_VAULT_PATH=C:\\real\\Notes\n",
        encoding="utf-8",
    )
    assert doctor.duplicate_env_keys(env) == ["YOYO_VAULT_PATH"]


def test_a_clean_env_reports_no_duplicates(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n", encoding="utf-8")
    assert doctor.duplicate_env_keys(env) == []


def test_comments_and_blank_lines_are_not_keys(tmp_path):
    """A commented-out old value is a note to yourself, not a duplicate — flagging it would
    train the owner to ignore the check."""
    env = tmp_path / ".env"
    env.write_text(
        "# YOYO_VAULT_PATH=C:\\old\n\nYOYO_VAULT_PATH=C:\\new\n   \n# another=comment\n",
        encoding="utf-8",
    )
    assert doctor.duplicate_env_keys(env) == []


def test_a_missing_env_is_not_an_error(tmp_path):
    assert doctor.duplicate_env_keys(tmp_path / "absent.env") == []


def test_the_env_check_fails_and_names_the_duplicated_key(monkeypatch):
    monkeypatch.setattr(doctor, "duplicate_env_keys", lambda path=None: ["YOYO_VAULT_PATH"])
    got = doctor._env()
    assert got.ok is False
    assert "YOYO_VAULT_PATH" in got.detail
    assert "LAST occurrence wins" in got.detail
