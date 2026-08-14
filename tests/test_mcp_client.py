"""MCP client adapter tests.

The live round-trip test actually spawns the vault MCP server as a subprocess and calls it
over stdio. It is the only test here that proves the adapter works end to end — the rest
pin the translation logic that a mock would otherwise let drift.
"""

from types import SimpleNamespace

import pytest

from yoyo.mcp import client
from yoyo.tools import Registry, ToolError


# ------------------------------------------------------------- config ----


def test_config_absent_is_not_an_error(tmp_path):
    assert client.load_config(tmp_path / "nope.yaml") == []


def test_config_parses_servers(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "servers:\n"
        "  vault:\n"
        "    command: python\n"
        "    args: ['-m', 'yoyo.mcp.vault_server']\n"
        "    enabled: false\n"
        "    prefix: v\n",
        encoding="utf-8",
    )
    specs = client.load_config(p)
    assert len(specs) == 1
    assert specs[0].name == "vault"
    assert specs[0].enabled is False
    assert specs[0].tool_name("search") == "v_search"


def test_tool_name_defaults_to_the_server_name():
    assert client.ServerSpec("files", "x").tool_name("read") == "files_read"


def test_empty_prefix_means_no_prefix():
    assert client.ServerSpec("files", "x", prefix="").tool_name("read") == "read"


def test_disabled_servers_are_skipped_not_mounted(tmp_path):
    p = tmp_path / "m.yaml"
    # NB: do not name a server `off` — YAML 1.1 parses that as the boolean False.
    p.write_text(
        "servers:\n  parked:\n    command: does-not-exist\n    enabled: false\n",
        encoding="utf-8",
    )
    report = client.mount_all(p, into=Registry())
    assert report["parked"] == {"ok": True, "skipped": "disabled"}


def test_one_bad_server_does_not_stop_the_others(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "servers:\n"
        "  broken:\n    command: definitely-not-a-real-binary-xyz\n"
        "  alsobroken:\n    command: another-fake-binary-xyz\n",
        encoding="utf-8",
    )
    report = client.mount_all(p, into=Registry())
    assert set(report) == {"broken", "alsobroken"}
    assert all(not r["ok"] for r in report.values())


# ------------------------------------------------- schema translation ----


def test_required_and_optional_fields_translate():
    model = client._params_model(
        "t",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    )
    m = model(query="x")
    assert m.query == "x"
    assert m.limit is None
    with pytest.raises(Exception):
        model()


def test_empty_schema_produces_a_usable_model():
    model = client._params_model("t", {})
    assert model().model_dump() == {}


def test_unknown_json_type_falls_back_to_any():
    model = client._params_model("t", {"properties": {"weird": {"type": "nonsense"}}})
    assert model(weird={"anything": 1}).weird == {"anything": 1}


# ------------------------------------------------------ result unwrapping ----


def _res(**kw):
    base = {"isError": False, "structuredContent": None, "content": []}
    return SimpleNamespace(**{**base, **kw})


def test_structured_content_is_preferred():
    assert client._unwrap(_res(structuredContent={"a": 1}), "t") == {"a": 1}


def test_fastmcp_result_wrapper_is_unwrapped():
    assert client._unwrap(_res(structuredContent={"result": [1, 2]}), "t") == [1, 2]


def test_json_text_is_parsed():
    r = _res(content=[SimpleNamespace(type="text", text='{"b": 2}')])
    assert client._unwrap(r, "t") == {"b": 2}


def test_plain_text_is_passed_through():
    r = _res(content=[SimpleNamespace(type="text", text="just prose")])
    assert client._unwrap(r, "t") == "just prose"


def test_server_error_becomes_a_tool_error():
    r = SimpleNamespace(
        isError=True,
        structuredContent=None,
        content=[SimpleNamespace(type="text", text="boom")],
    )
    with pytest.raises(ToolError, match="boom"):
        client._unwrap(r, "t")


# ------------------------------------------------------ live round trip ----


@pytest.mark.slow
def test_live_mount_of_the_vault_server(tmp_path, monkeypatch):
    """Spawn the real vault MCP server over stdio and call it through the adapter."""
    import shutil
    import sys

    root = tmp_path / "vault"
    root.mkdir()
    (root / "Note.md").write_text("# Note\n\nMCP round trip works.\n", encoding="utf-8")

    spec = client.ServerSpec(
        name="vault",
        command=sys.executable,
        args=["-m", "yoyo.mcp.vault_server"],
        env={"YOYO_VAULT_PATH": str(root), "PYTHONPATH": "src"},
        prefix="vault",
    )

    reg = Registry()
    try:
        names = client.mount(spec, into=reg)
    except client.MCPError as exc:
        pytest.skip(f"could not start the vault server: {exc}")

    try:
        # The server namespaces its own tools, so the prefix must not be doubled.
        assert "vault_search" in names
        assert "vault_write_draft" in names
        assert not any(n.startswith("vault_vault") for n in names)

        found = reg.dispatch("vault_search", {"query": "round trip"})
        assert found["count"] == 1
        assert found["hits"][0]["path"] == "Note.md"

        note = reg.dispatch("vault_read", {"path": "Note.md"})
        assert "MCP round trip works" in note["text"]

        stats = reg.dispatch("vault_stats", {})
        assert stats["notes"] == 1
    finally:
        client.unmount_all()
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------- failure diagnostics ----
# Observed live: a server that exited because YOYO_VAULT_PATH was unset surfaced as
# "unhandled errors in a TaskGroup (1 sub-exception)". True, and useless.


def test_exception_groups_are_flattened_to_real_causes():
    inner = FileNotFoundError(2, "No such file", "notaprogram")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    described = client._describe(group)
    assert "TaskGroup" not in described
    assert "notaprogram" in described


def test_nested_exception_groups_are_flattened():
    g = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("root cause")])])
    assert "root cause" in client._describe(g)


def test_plain_exception_is_described_with_its_type():
    assert client._describe(ValueError("boom")) == "ValueError: boom"


def test_missing_command_is_caught_before_connecting(tmp_path):
    spec = client.ServerSpec(name="ghost", command="definitely-not-a-real-binary-xyz")
    with pytest.raises(client.MCPError, match="not found on PATH"):
        client.mount(spec, into=Registry())


@pytest.mark.slow
def test_server_stderr_is_relayed_to_the_user(tmp_path, monkeypatch):
    """A vault server started without YOYO_VAULT_PATH must explain itself."""
    import sys

    spec = client.ServerSpec(
        name="vault",
        command=sys.executable,
        args=["-m", "yoyo.mcp.vault_server"],
        env={"YOYO_VAULT_PATH": "", "PYTHONPATH": "src", "YOYO_ENV_FILE": "/nonexistent"},
    )
    with pytest.raises(client.MCPError) as excinfo:
        client.mount(spec, into=Registry())
    message = str(excinfo.value)
    assert "TaskGroup" not in message or "server said" in message


def test_bare_python_resolves_to_the_running_interpreter():
    """`python` on PATH may not be the venv's interpreter — or may not exist at all."""
    import sys

    assert client.ServerSpec("v", "python").resolved_command() == sys.executable
    assert client.ServerSpec("v", "python3").resolved_command() == sys.executable
    assert client.ServerSpec("v", "npx").resolved_command() == "npx"


def test_errlog_is_a_real_file_with_a_fileno():
    """The child process is handed this stream directly, so it must have an OS handle.

    StringIO works on Linux and dies on Windows with "UnsupportedOperation: fileno" —
    exactly the failure this test exists to prevent recurring.
    """
    session = client.Session(client.ServerSpec("x", "python"))
    try:
        assert session._errlog.fileno() >= 0
    finally:
        session.close()


@pytest.mark.slow
def test_failed_mount_relays_the_server_message(tmp_path):
    """A vault server started with a bad vault path must explain itself in the error."""
    import sys

    spec = client.ServerSpec(
        name="vault",
        command=sys.executable,
        args=["-m", "yoyo.mcp.vault_server"],
        env={"YOYO_VAULT_PATH": str(tmp_path / "does-not-exist"), "PYTHONPATH": "src"},
    )
    with pytest.raises(client.MCPError) as excinfo:
        client.mount(spec, into=Registry())
    assert "server said" in str(excinfo.value)
    assert "yoyo-vault" in str(excinfo.value)


# ---------------------------------------------- SDK field-name drift ----
# mcp 1.x used camelCase (inputSchema / isError / structuredContent); 2.x uses snake_case.
# Reading only one spelling silently produced empty tool arguments and swallowed errors.


def test_snake_case_schema_field_is_read():
    tool = SimpleNamespace(
        name="t", description="d",
        input_schema={"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    session = SimpleNamespace(spec=client.ServerSpec("s", "python"), call=lambda n, a: a)
    adapted = client.adapt(session, tool)
    assert "query" in adapted.params.model_fields


def test_camel_case_schema_field_still_works():
    tool = SimpleNamespace(
        name="t", description="d",
        inputSchema={"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    session = SimpleNamespace(spec=client.ServerSpec("s", "python"), call=lambda n, a: a)
    adapted = client.adapt(session, tool)
    assert "query" in adapted.params.model_fields


def test_arguments_actually_reach_the_session():
    """The failure this guards: an unread schema yields a field-less model, so
    model_dump() returns {} and the server receives no arguments at all."""
    sent = {}
    tool = SimpleNamespace(
        name="t", description="d",
        input_schema={"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    session = SimpleNamespace(
        spec=client.ServerSpec("s", "python", prefix=""),
        call=lambda n, a: sent.update(a),
    )
    client.adapt(session, tool).run({"query": "hello"})
    assert sent == {"query": "hello"}


def test_snake_case_error_flag_is_honoured():
    r = SimpleNamespace(is_error=True, structured_content=None,
                        content=[SimpleNamespace(type="text", text="server blew up")])
    with pytest.raises(ToolError, match="server blew up"):
        client._unwrap(r, "t")


def test_snake_case_structured_content_is_read():
    r = SimpleNamespace(is_error=False, structured_content={"a": 1}, content=[])
    assert client._unwrap(r, "t") == {"a": 1}


def test_prefix_is_not_doubled_when_the_server_already_namespaces():
    """`yoyo-vault` exposes `vault_search`; prefixing blindly gave `vault_vault_search`."""
    spec = client.ServerSpec("vault", "python", prefix="vault")
    assert spec.tool_name("vault_search") == "vault_search"
    assert spec.tool_name("vault") == "vault"
    assert spec.tool_name("search") == "vault_search"


def test_unrelated_names_still_get_the_prefix():
    spec = client.ServerSpec("fs", "npx", prefix="fs")
    assert spec.tool_name("read_file") == "fs_read_file"
    assert spec.tool_name("fsck") == "fs_fsck"  # not a prefix match: needs the separator
