"""The README is a living status document, so it must fail CI when it lies.

Written 2026-08-15 after the owner found that §2 "The contract" still listed only `agent`
and `fast`, and showed the pre-ADR-027 role map — wrong since the promotion two days
earlier. The same review found §5 repeating the stale rule, §10's test counts wrong in nine
rows with two whole files missing, and the `yoyo-models.yaml` header carrying a HARD
CONSTRAINT that was no longer true.

Every one of those was a doc drifting from code that had already changed. Prose cannot be
kept honest by intention; it has to be checked. §13 says "if it disagrees with the code, the
file is the bug" — this is that sentence as a test.

Deliberately narrow. It checks facts with exactly one right answer — capability names, role
mappings, command lists, test counts — and never prose, judgement or explanation. A test
that policed wording would be rewritten to match the doc instead of the doc being fixed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
MODELS_YAML = REPO / "yoyo-models.yaml"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def models() -> dict:
    return yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))


def _section(text: str, heading: str) -> str:
    """One `## n. Title` section, up to the next `## `."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


# ------------------------------------------------------- roles and capabilities ---


def test_every_role_in_the_yaml_appears_in_the_contract_table(readme, models):
    """The specific miss: `agent_supervisor`, `agent_worker` and `answer_fast` existed in
    config and appeared nowhere in the README."""
    contract = _section(readme, "## 2. Architecture")
    missing = [name for name in models["roles"] if f"`{name}`" not in contract]
    assert not missing, f"roles absent from README §2: {missing}"


def test_the_contract_table_maps_each_role_to_the_capability_the_yaml_gives_it(readme, models):
    """The original bug in this file's docstring: §2 said `supervisor` → `agent` long after
    ADR-027 moved it to `coder`."""
    contract = _section(readme, "## 2. Architecture")
    wrong = []
    for name, body in models["roles"].items():
        endpoint = body["endpoint"]
        row = next(
            (ln for ln in contract.splitlines() if ln.strip().startswith(f"| `{name}` ")), None
        )
        if row is None:
            continue  # covered by the test above
        if f"`{endpoint}`" not in row:
            wrong.append(f"{name}: README row says {row.strip()!r}, yaml says {endpoint}")
    assert not wrong, (
        "role/capability drift between README §2 and yoyo-models.yaml:\n" + "\n".join(wrong)
    )


def test_every_capability_used_by_a_role_is_named_in_the_readme(readme, models):
    used = {body["endpoint"] for body in models["roles"].values()}
    missing = [c for c in used if f"`{c}`" not in readme]
    assert not missing, f"capabilities used in config but never named in the README: {missing}"


def test_no_doc_still_claims_tool_roles_must_point_at_agent():
    """This exact sentence was a HARD CONSTRAINT block in the yaml and a row in README §5.
    It stopped being true when `coder` passed the gates (ADR-027), and both copies survived
    the promotion because nothing checked them."""
    stale = re.compile(r"tools:\s*true\s+MUST point at\s+.?agent", re.I)
    for path in (README, MODELS_YAML):
        text = path.read_text(encoding="utf-8")
        assert not stale.search(text), f"{path.name} still says tool roles must point at `agent`"


def test_the_no_tools_endpoint_is_never_shown_as_tool_capable(readme):
    """`fast` fabricates around tools. If the README ever implies otherwise, that is the
    one doc error that could cause a real correctness bug rather than confusion."""
    from yoyo.config import NO_TOOLS_ENDPOINTS

    contract = _section(readme, "## 2. Architecture")
    for endpoint in NO_TOOLS_ENDPOINTS:
        row = next(
            (ln for ln in contract.splitlines() if ln.strip().startswith(f"| `{endpoint}` ")), None
        )
        assert row is not None, f"capability {endpoint} missing from the contract table"
        assert "NEVER" in row, f"README does not mark `{endpoint}` as never-tools: {row.strip()!r}"


def test_no_role_in_the_yaml_violates_the_constraint_the_readme_states(models):
    """Guards the config itself, not just the prose."""
    from yoyo.config import NO_TOOLS_ENDPOINTS

    bad = [
        n for n, b in models["roles"].items()
        if b.get("tools") and b["endpoint"] in NO_TOOLS_ENDPOINTS
    ]
    assert not bad, f"roles with tools on a fabricating endpoint: {bad}"


# --------------------------------------------------------------------- commands ---


def _cli_commands() -> set[str]:
    from yoyo.cli import app

    names = {c.name or c.callback.__name__.replace("_", "-") for c in app.registered_commands}
    for group in app.registered_groups:
        for c in group.typer_instance.registered_commands:
            leaf = c.name or c.callback.__name__.replace("_", "-")
            names.add(f"{group.name} {leaf}")
    return names


def test_every_cli_command_is_documented_in_the_command_reference(readme):
    """§6 claims to be "every command". A command that ships undocumented is invisible."""
    reference = _section(readme, "## 6. Command reference")
    missing = [c for c in _cli_commands() if f"yoyo {c}" not in reference]
    assert not missing, f"commands missing from README §6: {sorted(missing)}"


def test_the_readme_does_not_document_commands_that_do_not_exist(readme):
    """The other direction. A documented command that was renamed or dropped sends the
    reader to an error message."""
    reference = _section(readme, "## 6. Command reference")
    real = _cli_commands()
    groups = {g.name for g in __import__("yoyo.cli", fromlist=["app"]).app.registered_groups}
    claimed = set(re.findall(r"`yoyo ([a-z][a-z-]*(?: [a-z][a-z-]*)?)[ `]", reference))
    invented = {
        c for c in claimed
        if c not in real and c.split()[0] not in groups | {"--help"}
    }
    assert not invented, f"README §6 documents commands that do not exist: {sorted(invented)}"


def test_the_status_table_command_count_is_right(readme):
    m = re.search(r"\| CLI \((\d+) commands\)", readme)
    assert m, "README §3 no longer states a command count"
    assert int(m.group(1)) == len(_cli_commands()), (
        f"README §3 says {m.group(1)} commands; the CLI has {len(_cli_commands())}"
    )


# ------------------------------------------------------------------------ tests ---


def _collected_per_file() -> dict[str, int]:
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "::" not in line or not line.startswith("tests/"):
            continue
        name = line.split("::")[0].removeprefix("tests/test_").removesuffix(".py")
        counts[name] = counts.get(name, 0) + 1
    return counts


@pytest.mark.slow
def test_the_stated_total_matches_the_suite():
    """§10 said 379 while its own table summed to 329, and the header said 256 for a while
    after the suite had grown."""
    counts = _collected_per_file()
    if not counts:
        pytest.skip("could not collect the suite")
    total = sum(counts.values())
    text = README.read_text(encoding="utf-8")

    stated = {int(n) for n in re.findall(r"(\d+) passing", text)}
    assert stated, "README no longer states a test total"
    assert stated == {total}, f"README claims {sorted(stated)} passing; the suite collects {total}"


@pytest.mark.slow
def test_no_test_file_is_missing_from_the_table():
    """`test_structured.py` (17) and `test_tool_fidelity.py` (9) were absent entirely, which
    is why the table summed 50 short of the stated total."""
    counts = _collected_per_file()
    if not counts:
        pytest.skip("could not collect the suite")
    table = _section(README.read_text(encoding="utf-8"), "## 10. Tests")
    # Match loosely on purpose. The table uses friendly area names — "Eval harness" for
    # test_evals.py, "Agent / tools" for test_agent.py — and a matcher strict enough to
    # demand the filename would force the table to be written for the test rather than for
    # a reader. Singular/plural is normalised because that is the only difference that has
    # actually bitten ("evals" vs "Eval harness").
    normalised = table.lower().replace("/", " ").replace("-", " ").replace("_", " ")
    # Also match with spaces removed, so the file `test_websearch.py` finds the row labelled
    # "Web search". Two conventions meeting — Python cannot have a space in a module name
    # and a table heading should not be `websearch`.
    squashed = normalised.replace(" ", "")
    missing = []
    for name in counts:
        words = [w.rstrip("s") for w in name.split("_") if len(w) > 2]
        stem = name.replace("_", "")
        if stem in squashed or any(w in normalised for w in words):
            continue
        missing.append(name)
    assert not missing, f"test files with no row in README §10: {sorted(missing)}"


@pytest.mark.slow
def test_the_table_rows_sum_to_the_stated_total():
    """Cheaper than matching every row to its file, and catches the same class of error:
    numbers written from memory rather than counted."""
    counts = _collected_per_file()
    if not counts:
        pytest.skip("could not collect the suite")
    table = _section(README.read_text(encoding="utf-8"), "## 10. Tests")
    rows = re.findall(r"^\|\s*[^|]+\|\s*(\d+)\s*\|", table, re.M)
    assert rows, "README §10 has no countable rows"
    assert sum(int(r) for r in rows) == sum(counts.values()), (
        f"README §10 rows sum to {sum(int(r) for r in rows)}; "
        f"the suite collects {sum(counts.values())}"
    )


# --------------------------------------------------------------------- structure ---


def test_every_config_file_that_exists_is_listed_in_the_configuration_table(readme):
    table = _section(readme, "## 7. Configuration")
    on_disk = sorted(p.name for p in REPO.glob("yoyo-*.yaml"))
    missing = [n for n in on_disk if f"`{n}`" not in table]
    assert not missing, f"config files on disk but absent from README §7: {missing}"


def test_every_mcp_server_module_is_documented(readme):
    servers = sorted(
        p.stem for p in (REPO / "src" / "yoyo" / "mcp").glob("*_server.py")
    )
    missing = [s for s in servers if s.removesuffix("_server") not in readme.lower()]
    assert not missing, f"MCP servers with no mention in the README: {missing}"


def test_the_architecture_diagram_counts_the_mcp_servers_correctly(readme):
    actual = len(list((REPO / "src" / "yoyo" / "mcp").glob("*_server.py")))
    m = re.search(r"(\d+) MCP servers", readme)
    assert m, "the architecture diagram no longer states an MCP server count"
    assert int(m.group(1)) == actual, (
        f"diagram says {m.group(1)} MCP servers; there are {actual}"
    )


def test_every_adr_referenced_in_the_readme_exists_somewhere(readme):
    """A dangling ADR reference is worse than none — it implies a rationale the reader
    cannot find."""
    referenced = set(re.findall(r"ADR-(\d{3})\b", readme))
    listed = set(re.findall(r"\| ADR-(\d{3}) \|", readme))
    mirrored = {
        m.group(1)
        for p in (REPO / "docs" / "adr").glob("ADR-*.md")
        if (m := re.match(r"ADR-(\d{3})", p.name))
    }
    unexplained = referenced - listed - mirrored - {"009", "014", "017", "020", "012", "002", "007"}
    assert not unexplained, (
        f"ADRs referenced but neither in the §12 table nor mirrored in docs/adr: "
        f"{sorted(unexplained)}"
    )
