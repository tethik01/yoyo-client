"""Tool registry and agent-loop tests — all offline, with a scripted model.

The loop is where budget governance lives. Local inference has no dollar cost, so nothing
naturally stops runaway recursion; these tests prove the brakes work.
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from yoyo import agent, llm
from yoyo.tools import Registry, Tool, ToolError


class EchoArgs(BaseModel):
    value: str = Field(description="anything")


def _registry(fn=None, name="echo"):
    reg = Registry()
    reg.add(
        Tool(
            name=name,
            description="Echo a value back.",
            params=EchoArgs,
            fn=fn or (lambda a: {"echoed": a.value}),
        )
    )
    return reg


def _call(name, arguments, cid="c1"):
    return SimpleNamespace(
        id=cid, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _scripted(monkeypatch, turns):
    """Replace llm.chat with a fixed sequence of responses."""
    seen = []

    def fake_chat(messages, role="answer", **kw):
        seen.append({"messages": list(messages), "role": role, "tools": kw.get("tools")})
        turn = turns[min(len(seen) - 1, len(turns) - 1)]
        return llm.ChatResult(
            text=turn.get("text", ""),
            model="test-model",
            role=role,
            endpoint="agent",
            reasoning=None,
            prompt_tokens=None,
            completion_tokens=None,
            tool_calls=turn.get("tool_calls"),
        )

    monkeypatch.setattr(agent.llm, "chat", fake_chat)
    return seen


# ------------------------------------------------------------- registry ----


def test_spec_shape_is_openai_compatible():
    spec = _registry().specs()[0]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "echo"
    assert "value" in spec["function"]["parameters"]["properties"]


def test_specs_can_be_filtered():
    reg = _registry()
    reg.add(Tool("other", "d", EchoArgs, lambda a: None))
    assert len(reg.specs()) == 2
    assert len(reg.specs(["echo"])) == 1


def test_unknown_tool_lists_what_exists():
    with pytest.raises(ToolError, match="Registered: echo"):
        _registry().dispatch("nope", {})


def test_invalid_arguments_are_rejected_not_coerced():
    with pytest.raises(ToolError, match="invalid arguments"):
        _registry().dispatch("echo", {"wrong_field": 1})


def test_duplicate_registration_is_an_error():
    reg = Registry()

    @reg.register("dup", "d", EchoArgs)
    def _a(args):
        return None

    with pytest.raises(ValueError, match="already registered"):

        @reg.register("dup", "d", EchoArgs)
        def _b(args):
            return None


# ----------------------------------------------------------- agent loop ----


def test_answer_without_tool_calls_returns_immediately(monkeypatch):
    seen = _scripted(monkeypatch, [{"text": "done"}])
    r = agent.run("q", tool_registry=_registry())
    assert r.text == "done"
    assert r.iterations == 1
    assert r.invocations == []
    assert len(seen) == 1


def test_tool_call_is_executed_and_fed_back(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "hi"}')]},
            {"text": "the tool said hi"},
        ],
    )
    r = agent.run("q", tool_registry=_registry())
    assert r.called("echo")
    assert r.invocations[0].result == {"echoed": "hi"}
    assert r.text == "the tool said hi"
    # Second call must include the assistant tool_calls turn and the tool result.
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


def test_tool_error_is_surfaced_to_the_model_not_raised(monkeypatch):
    """A good model retries. Swallowing the error would hide the difference."""

    def boom(args):
        raise ToolError("transient failure")

    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "x"}')]},
            {"text": "recovered"},
        ],
    )
    r = agent.run("q", tool_registry=_registry(boom))
    assert r.invocations[0].ok is False
    assert "transient failure" in r.invocations[0].error
    assert r.text == "recovered"


def test_unexpected_exception_does_not_kill_the_turn(monkeypatch):
    def boom(args):
        raise ZeroDivisionError("bad math")

    _scripted(
        monkeypatch,
        [{"tool_calls": [_call("echo", '{"value": "x"}')]}, {"text": "ok"}],
    )
    r = agent.run("q", tool_registry=_registry(boom))
    assert r.invocations[0].ok is False
    assert "ZeroDivisionError" in r.invocations[0].error


def test_malformed_arguments_json_is_reported(monkeypatch):
    _scripted(
        monkeypatch,
        [{"tool_calls": [_call("echo", "{not json")]}, {"text": "ok"}],
    )
    r = agent.run("q", tool_registry=_registry())
    assert r.invocations[0].ok is False
    assert "not valid JSON" in r.invocations[0].error


def test_iteration_budget_stops_a_runaway_loop(monkeypatch):
    """A model that calls a tool forever must be stopped by us, not by a bill."""
    _scripted(monkeypatch, [{"tool_calls": [_call("echo", '{"value": "x"}')]}])
    r = agent.run("q", max_iterations=3, tool_registry=_registry())
    assert r.iterations == 3
    assert "iteration budget" in r.stopped_because
    assert len(r.invocations) == 3


def test_wall_clock_budget_stops_the_loop(monkeypatch):
    _scripted(monkeypatch, [{"tool_calls": [_call("echo", '{"value": "x"}')]}])
    r = agent.run("q", max_iterations=50, wall_clock_s=0, tool_registry=_registry())
    assert "wall-clock" in r.stopped_because


def test_parallel_tool_calls_in_one_turn_all_execute(monkeypatch):
    _scripted(
        monkeypatch,
        [
            {
                "tool_calls": [
                    _call("echo", '{"value": "a"}', "c1"),
                    _call("echo", '{"value": "b"}', "c2"),
                ]
            },
            {"text": "both"},
        ],
    )
    r = agent.run("q", tool_registry=_registry())
    assert [i.result["echoed"] for i in r.invocations] == ["a", "b"]


def test_tools_are_actually_passed_to_the_model(monkeypatch):
    seen = _scripted(monkeypatch, [{"text": "x"}])
    agent.run("q", tool_registry=_registry())
    assert seen[0]["tools"][0]["function"]["name"] == "echo"


def test_agent_uses_a_tool_capable_role_by_default():
    """The default must not be a role the tool guard would reject.

    Asserts the PROPERTY (tool-capable, not a fabricating endpoint), not which model is
    currently pinned. The previous version asserted `endpoint == "agent"` and broke the
    moment a better model was promoted — a test that fails on an intended change is noise.
    """
    from yoyo.config import NO_TOOLS_ENDPOINTS, get_models

    role = get_models().role("supervisor")
    assert role.tools is True
    assert role.endpoint not in NO_TOOLS_ENDPOINTS


def test_budget_exhaustion_still_produces_an_answer(monkeypatch):
    """Stopping mid-tool-loop must not return an empty string.

    Observed live: the loop hit its iteration budget after six tool calls and returned
    nothing, because the last assistant turn was a tool call. The work was done and the
    user saw none of it.
    """
    calls = {"n": 0}

    def fake_chat(messages, role="answer", **kw):
        calls["n"] += 1
        # Every turn WITH tools keeps calling the tool; the turn without tools answers.
        if kw.get("tools"):
            return llm.ChatResult(
                text="", model="m", role=role, endpoint="agent", reasoning=None,
                prompt_tokens=None, completion_tokens=None,
                tool_calls=[_call("echo", '{"value": "x"}')],
            )
        return llm.ChatResult(
            text="Partial answer from what I gathered.", model="m", role=role,
            endpoint="agent", reasoning=None, prompt_tokens=None,
            completion_tokens=None, tool_calls=None,
        )

    monkeypatch.setattr(agent.llm, "chat", fake_chat)
    r = agent.run("q", max_iterations=2, tool_registry=_registry())

    assert r.text == "Partial answer from what I gathered."
    assert "iteration budget" in r.stopped_because
    assert "partial results" in r.stopped_because


def test_final_answer_turn_is_sent_without_tools(monkeypatch):
    seen = []

    def fake_chat(messages, role="answer", **kw):
        seen.append(kw.get("tools"))
        if kw.get("tools"):
            return llm.ChatResult(
                text="", model="m", role=role, endpoint="agent", reasoning=None,
                prompt_tokens=None, completion_tokens=None,
                tool_calls=[_call("echo", '{"value": "x"}')],
            )
        return llm.ChatResult(
            text="done", model="m", role=role, endpoint="agent", reasoning=None,
            prompt_tokens=None, completion_tokens=None, tool_calls=None,
        )

    monkeypatch.setattr(agent.llm, "chat", fake_chat)
    agent.run("q", max_iterations=2, tool_registry=_registry())
    assert seen[-1] is None, "the final answer turn must not offer tools again"


def test_no_forced_answer_when_the_loop_completed_normally(monkeypatch):
    seen = _scripted(monkeypatch, [{"text": "clean answer"}])
    r = agent.run("q", tool_registry=_registry())
    assert r.stopped_because == "completed"
    assert len(seen) == 1


def test_no_forced_answer_when_no_tools_were_ever_called(monkeypatch):
    """Budget exhausted without a single tool call means something else is wrong;
    do not spend another model call papering over it."""
    _scripted(monkeypatch, [{"text": ""}])
    r = agent.run("q", max_iterations=1, tool_registry=_registry())
    assert "partial results" not in r.stopped_because


# ------------------------------------------------- duplicate-call guard ----
# Observed live: workers reworded the same search three or four times and burned their whole
# budget. Prompting them not to did not work; this is the mechanical fix.


def test_reworded_duplicate_is_short_circuited(monkeypatch):
    hits = {"n": 0}

    def counting(args):
        hits["n"] += 1
        return {"echoed": args.value}

    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "GB10 box"}', "c1")]},
            {"tool_calls": [_call("echo", '{"value": "box  GB10!"}', "c2")]},
            {"text": "done"},
        ],
    )
    r = agent.run("q", tool_registry=_registry(counting))

    assert hits["n"] == 1, "the reworded repeat must not re-execute the tool"
    assert len(r.invocations) == 2, "but it is still recorded as a call the model made"
    assert all(i.ok for i in r.invocations)


def test_duplicate_result_is_labelled_as_a_repeat(monkeypatch):
    """A silently cached result looks like a fresh search that found the same thing."""
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "a"}', "c1")]},
            {"tool_calls": [_call("echo", '{"value": "a"}', "c2")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_registry())
    tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert "already made this exact call" in tool_msgs[-1]["content"]


def test_different_queries_are_not_treated_as_duplicates(monkeypatch):
    hits = {"n": 0}

    def counting(args):
        hits["n"] += 1
        return {"echoed": args.value}

    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "invoice from alice"}', "c1")]},
            {"tool_calls": [_call("echo", '{"value": "quarterly review"}', "c2")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_registry(counting))
    assert hits["n"] == 2


def test_overusing_one_tool_adds_a_stop_hint(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "one"}', "c1")]},
            {"tool_calls": [_call("echo", '{"value": "two"}', "c2")]},
            {"tool_calls": [_call("echo", '{"value": "three"}', "c3")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_registry())
    tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert "3 times this turn" in tool_msgs[-1]["content"]


def test_no_stop_hint_before_the_limit(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "one"}', "c1")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_registry())
    tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert "times this turn" not in tool_msgs[-1]["content"]


def test_cache_does_not_leak_between_turns(monkeypatch):
    """Relevance is scoped to one question; a stale answer to a new question is worse
    than a repeated search."""
    hits = {"n": 0}

    def counting(args):
        hits["n"] += 1
        return {"echoed": args.value}

    reg = _registry(counting)
    for _ in range(2):
        _scripted(
            monkeypatch,
            [{"tool_calls": [_call("echo", '{"value": "same"}')]}, {"text": "done"}],
        )
        agent.run("q", tool_registry=reg)
    assert hits["n"] == 2


def test_failed_calls_are_not_cached(monkeypatch):
    """A transient failure must be retryable — that is the whole point of the retry gate."""
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ToolError("transient")
        return {"echoed": args.value}

    _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("echo", '{"value": "x"}', "c1")]},
            {"tool_calls": [_call("echo", '{"value": "x"}', "c2")]},
            {"text": "recovered"},
        ],
    )
    r = agent.run("q", tool_registry=_registry(flaky))
    assert calls["n"] == 2, "the retry must actually re-execute"
    assert r.invocations[0].ok is False
    assert r.invocations[1].ok is True


# ------------------------------------------- source tunnelling (2026-08-15) ----
# Regression class observed live: asked a two-part question, the model called vault_search
# four times with reworded queries, never called search_corpus, and reported the second half
# as "could not find any mention" when the answer was in the corpus. The existing brakes did
# not catch it — the calls were not duplicates (different wording, different results) and the
# stop hint fires at 3, which is the wrong advice anyway when half the question is unanswered.


def _two_source_registry():
    reg = Registry()
    for name in ("vault_search", "search_corpus"):
        reg.add(
            Tool(
                name=name,
                description=f"{name} description",
                params=EchoArgs,
                fn=lambda a, n=name: {"from": n, "hit": a.value},
            )
        )
    return reg


def _last_tool_content(seen):
    return [m for m in seen[-1]["messages"] if m["role"] == "tool"][-1]["content"]


def test_second_call_to_one_source_names_the_untried_source(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("vault_search", '{"value": "GB10 box"}', "c1")]},
            {"tool_calls": [_call("vault_search", '{"value": "bake-off"}', "c2")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_two_source_registry())
    assert "search_corpus" in _last_tool_content(seen)


def test_the_hint_does_not_name_the_tool_already_being_used(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("vault_search", '{"value": "a"}', "c1")]},
            {"tool_calls": [_call("vault_search", '{"value": "b"}', "c2")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_two_source_registry())
    content = _last_tool_content(seen)
    assert "not yet tried: search_corpus" in content


def test_no_hint_when_every_source_has_been_used(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("search_corpus", '{"value": "a"}', "c1")]},
            {"tool_calls": [_call("vault_search", '{"value": "b"}', "c2")]},
            {"tool_calls": [_call("vault_search", '{"value": "c"}', "c3")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_two_source_registry())
    assert "not yet tried" not in _last_tool_content(seen)


def test_first_call_carries_no_hint(monkeypatch):
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("vault_search", '{"value": "a"}', "c1")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_two_source_registry())
    assert "not yet tried" not in _last_tool_content(seen)


def test_stop_hint_still_wins_at_the_hard_limit(monkeypatch):
    """At 3 calls the advice must be 'stop', not 'try elsewhere' — otherwise the two nudges
    fight each other and the budget goes on searching instead of answering."""
    seen = _scripted(
        monkeypatch,
        [
            {"tool_calls": [_call("vault_search", '{"value": "a"}', "c1")]},
            {"tool_calls": [_call("vault_search", '{"value": "b"}', "c2")]},
            {"tool_calls": [_call("vault_search", '{"value": "c"}', "c3")]},
            {"text": "done"},
        ],
    )
    agent.run("q", tool_registry=_two_source_registry())
    content = _last_tool_content(seen)
    assert "3 times this turn" in content
    assert "not yet tried" not in content


# ------------------------------------------ fabricated citations in answers ----


def test_fabricated_path_is_stripped_from_the_answer(monkeypatch):
    _scripted(
        monkeypatch,
        [{"text": "See [MyAIServer.md](file:///Users/nobody/Vault/MyAIServer.md) for detail."}],
    )
    result = agent.run("q", tool_registry=_two_source_registry())
    assert "file:///" not in result.text
    assert "[MyAIServer.md]" in result.text
    assert result.fabricated_links == ["file:///"]


def test_clean_answer_reports_no_fabrication(monkeypatch):
    _scripted(monkeypatch, [{"text": "See [MyAIServer.md] and chunk [7]."}])
    result = agent.run("q", tool_registry=_two_source_registry())
    assert result.fabricated_links == []
    assert result.text == "See [MyAIServer.md] and chunk [7]."


def test_prompt_forbids_reporting_absence_from_one_source():
    """The wrong answer was a scope error, not a lookup error: 'not in the notes' was
    reported as 'not there'. The prompt has to name that distinction explicitly."""
    assert "is not the same claim as" in agent.SYSTEM_PROMPT
    assert "several parts" in agent.SYSTEM_PROMPT
