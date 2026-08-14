"""The ADR-023 constraint, tested.

`fast` (qwen3.6) was measured over four trials: 3/4 called the tool once, errored and gave
up without retrying; 1/4 never called it and fabricated a plausible answer. Any code path
that sends tools to `fast` is a correctness bug, so it must raise — not warn, not degrade.
"""

import pytest

from yoyo import llm
from yoyo.config import Role, load_model_config

TOOLS = [{"type": "function", "function": {"name": "get_price", "parameters": {}}}]


def test_config_rejects_tools_on_fast_at_load():
    bad = Role(name="oops", endpoint="fast", tools=True)
    with pytest.raises(ValueError, match="not tool-reliable"):
        bad.check()


def test_config_allows_tools_on_agent():
    Role(name="worker", endpoint="agent", tools=True).check()


def test_guard_raises_for_fast_endpoint():
    role = Role(name="summarize", endpoint="fast", tools=False)
    with pytest.raises(llm.ToolFidelityError, match="fabricates"):
        llm._guard_tools(role, TOOLS)


def test_guard_raises_when_role_not_tool_enabled():
    """Even on `agent`, a role that didn't declare tools shouldn't silently receive them."""
    role = Role(name="reader", endpoint="agent", tools=False)
    with pytest.raises(llm.ToolFidelityError):
        llm._guard_tools(role, TOOLS)


def test_guard_allows_declared_tool_role():
    role = Role(name="worker", endpoint="agent", tools=True)
    llm._guard_tools(role, TOOLS)  # must not raise


def test_guard_is_a_noop_without_tools():
    llm._guard_tools(Role(name="summarize", endpoint="fast"), None)
    llm._guard_tools(Role(name="summarize", endpoint="fast"), [])


def test_shipped_config_has_no_tool_roles_on_fast():
    """Guards the actual yoyo-models.yaml in the repo, not a fixture."""
    cfg = load_model_config()
    offenders = [n for n, r in cfg.roles.items() if r.tools and r.endpoint == "fast"]
    assert offenders == [], f"tool-using roles pointed at 'fast': {offenders}"


def test_shipped_config_has_at_least_one_tool_role():
    cfg = load_model_config()
    assert any(r.tools for r in cfg.roles.values()), "no tool-capable role defined"


def test_context_budget_fits_server_ceiling():
    """24k chars of sources ~= 6k tokens, well inside the 32K hard cap with room for the
    reasoning trace, which is on by default and can run to hundreds of tokens."""
    cfg = load_model_config()
    approx_tokens = cfg.retrieval.max_context_chars / 4
    assert approx_tokens < cfg.endpoint.context_ceiling * 0.5
