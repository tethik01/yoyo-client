"""Structured output through `llm.py`, validated with pydantic.

Deliberately NOT PydanticAI. That library wants to own the model client, which would create
a second path to MyAIServer — and `llm._guard_tools` only protects the first. The guard is
what stops `fast` being handed tools it fabricates around, so a second unguarded path is
exactly the wrong thing to add. One client, one guard, validation on top.

Retries on invalid output rather than failing the turn: local models produce near-miss JSON
often enough that one corrective round-trip is cheaper than losing the work.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from . import llm

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredError(RuntimeError):
    pass


def extract_json(text: str) -> str:
    """Pull JSON out of a model response.

    Thinking is on by default and models wrap output in prose or fences even when asked not
    to. Rather than fight that with prompt engineering, parse defensively.
    """
    if not text:
        raise StructuredError("empty response")

    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()

    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise StructuredError(f"no JSON found in: {text[:200]!r}")

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise StructuredError(f"unbalanced JSON in: {text[:200]!r}")


def generate(
    schema: type[T],
    instruction: str,
    *,
    role: str = "supervisor",
    context: str = "",
    max_attempts: int = MAX_ATTEMPTS,
    **kwargs: Any,
) -> T:
    """Ask for JSON matching `schema` and return a validated instance."""
    spec = json.dumps(schema.model_json_schema(), indent=2)
    system = (
        "You produce JSON and nothing else.\n\n"
        "Respond with a single JSON object matching this schema exactly. No prose before "
        "or after, no markdown fences, no explanation.\n\n"
        f"{spec}"
    )
    user = f"{context}\n\n---\n\n{instruction}" if context else instruction
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        result = llm.chat(messages, role=role, **kwargs)
        try:
            return schema.model_validate_json(extract_json(result.text))
        except (StructuredError, ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            log.warning("structured attempt %d/%d failed: %s", attempt, max_attempts, last_error)
            if attempt == max_attempts:
                break
            # Feed the failure back: a near-miss usually corrects in one round trip.
            messages.append({"role": "assistant", "content": result.text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That did not validate: {last_error}\n\n"
                        f"Return ONLY the corrected JSON object."
                    ),
                }
            )

    raise StructuredError(
        f"could not obtain valid {schema.__name__} after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
