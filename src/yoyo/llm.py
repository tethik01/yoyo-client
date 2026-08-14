"""The one place that talks to MyAIServer.

Every call goes through LiteLLM's OpenAI-compatible API over Tailscale. Yoyo code names a
ROLE ("supervisor", "answer", "extract"); this module resolves it to an endpoint capability
via yoyo-models.yaml. Model identity never appears in Yoyo code.

Three measured facts this module enforces or accounts for:

1. `fast` is not tool-reliable. Passing tools to it raises — see `_guard_tools`.
2. Only `agent` scales under concurrency (3.62x @ conc=4). `fast` serialises (1.13x).
   Do not fan out against `fast` expecting wall-clock savings.
3. Thinking is on by default and expensive. Responses carry `reasoning_content` alongside
   `content`; it is captured separately and never merged into the answer text.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx
from openai import OpenAI

from .config import NO_TOOLS_ENDPOINTS, Role, get_models, get_settings

log = logging.getLogger(__name__)

MAX_RETRIES_429 = 4


class LLMError(RuntimeError):
    pass


class ToolFidelityError(LLMError):
    """Raised when tools would be sent to a capability that is not tool-reliable."""


@dataclass(slots=True)
class ChatResult:
    text: str
    model: str
    role: str
    endpoint: str
    reasoning: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    tool_calls: list[Any] | None = None
    raw: Any = None


def _client() -> OpenAI:
    s = get_settings()
    if not s.llm_api_key:
        raise LLMError(
            "YOYO_LLM_API_KEY is unset. Copy .env.example to .env and paste the LiteLLM "
            "virtual key issued for this laptop."
        )
    return OpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        # Server-side timeout is 900 s; agent tool loops run to minutes.
        timeout=httpx.Timeout(s.request_timeout, connect=10.0),
        max_retries=0,  # we do our own, so 429 backoff is explicit and logged
    )


def resolve(role: str) -> Role:
    return get_models().role(role)


def _guard_tools(role: Role, tools: list[dict[str, Any]] | None) -> None:
    """The ADR-023 constraint, enforced in code rather than trusted to reviewers.

    Measured over four trials, `fast` (qwen3.6): 3/4 called the tool once, errored and gave
    up; 1/4 never called it and fabricated a plausible answer. This is a correctness
    failure, so it raises rather than warns.
    """
    if not tools:
        return
    if role.endpoint in NO_TOOLS_ENDPOINTS or not role.tools:
        raise ToolFidelityError(
            f"Refusing to send tools to role {role.name!r} (endpoint {role.endpoint!r}). "
            f"{role.endpoint!r} skips available tools and fabricates answers. Use a role "
            f"with tools: true pointed at 'agent' — e.g. 'supervisor' or 'worker'."
        )


def _call_with_backoff(fn, *, what: str):  # noqa: ANN001, ANN202
    """Per-key limits are real (max_parallel_requests ~2). 429 is expected, not exceptional."""
    delay = 1.0
    for attempt in range(MAX_RETRIES_429 + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if status != 429 or attempt == MAX_RETRIES_429:
                raise LLMError(f"{what} failed against {get_settings().llm_base_url}: {exc}") from exc
            sleep = delay + random.uniform(0, 0.5)
            log.warning("429 rate limited on %s; retrying in %.1fs (attempt %d)", what, sleep, attempt + 1)
            time.sleep(sleep)
            delay *= 2
    raise LLMError(f"{what}: exhausted retries")  # unreachable, keeps type checkers happy


def _build_kwargs(
    role: Role,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float | None,
    max_tokens: int | None,
    reasoning: str | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": role.endpoint, "messages": messages}

    t = temperature if temperature is not None else role.temperature
    if t is not None:
        kwargs["temperature"] = t
    mt = max_tokens if max_tokens is not None else role.max_tokens
    if mt is not None:
        kwargs["max_tokens"] = mt

    # `agent` supports controllable reasoning strength; low cut tokens ~38% at the same
    # tok/s. Worker roles should not be thinking hard.
    effort = reasoning or role.reasoning
    if effort:
        kwargs["reasoning_effort"] = effort

    if tools:
        kwargs["tools"] = tools
        kwargs.setdefault("tool_choice", "auto")

    kwargs.update(extra)
    return kwargs


def chat(
    messages: list[dict[str, Any]],
    role: str = "answer",
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning: str | None = None,
    **extra: Any,
) -> ChatResult:
    r = resolve(role)
    _guard_tools(r, tools)
    kwargs = _build_kwargs(r, messages, tools, temperature, max_tokens, reasoning, extra)

    resp = _call_with_backoff(
        lambda: _client().chat.completions.create(**kwargs), what=f"chat[{role}]"
    )

    msg = resp.choices[0].message
    usage = getattr(resp, "usage", None)
    return ChatResult(
        text=msg.content or "",
        model=resp.model,
        role=role,
        endpoint=r.endpoint,
        # Thinking is on by default. Captured, never merged into the answer.
        reasoning=getattr(msg, "reasoning_content", None),
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        tool_calls=getattr(msg, "tool_calls", None),
        raw=resp,
    )


def stream_chat(
    messages: list[dict[str, Any]],
    role: str = "answer",
    *,
    tools: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
    include_reasoning: bool = False,
    **extra: Any,
) -> Iterator[str]:
    """Stream answer text. Reasoning deltas are dropped unless explicitly requested.

    Streaming is strongly preferred over blocking calls: `agent` single turns run 30-60 s
    and tool loops 2-5 minutes.
    """
    r = resolve(role)
    _guard_tools(r, tools)
    kwargs = _build_kwargs(r, messages, tools, None, None, reasoning, extra)
    kwargs["stream"] = True

    try:
        stream = _client().chat.completions.create(**kwargs)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if include_reasoning:
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    yield rc
            if delta.content:
                yield delta.content
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"stream[{role}] failed against {get_settings().llm_base_url}: {exc}") from exc


def list_models() -> list[str]:
    """What LiteLLM actually exposes. Used by `yoyo doctor` to catch yaml drift."""
    resp = _call_with_backoff(lambda: _client().models.list(), what="models.list")
    return sorted(m.id for m in resp.data)
