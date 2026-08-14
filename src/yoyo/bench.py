"""Client-side benchmark: single-stream speed and concurrency scaling.

Exists because ADR-022 says concurrency is an **empirical** property that must be measured
per model — two architectural hypotheses were tested and both falsified. A new model's
scaling cannot be inferred from it being MoE, from its parameter count, or from what a
sibling model does.

Two details that are easy to get wrong and would invalidate the numbers:

- **Prompts must be distinct.** Identical prompts let prefix caching serve later requests
  from the first one's KV cache, which looks like superb scaling and measures nothing. The
  original bake-off used four distinct prompts for exactly this reason.
- **Per-key limits are real.** LiteLLM caps `max_parallel_requests` (~2 for this laptop's
  key). Hitting that produces queueing that looks like poor model scaling but is a policy
  limit. 429s are counted and reported separately so the two cannot be confused.
"""

from __future__ import annotations

import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import llm

log = logging.getLogger(__name__)

#: Distinct on purpose — see the module docstring. Each asks for a similar amount of output.
PROMPTS = [
    "Explain in exactly three sentences why a bicycle stays upright when moving.",
    "Describe in exactly three sentences how a lock and key mechanism works.",
    "Summarise in exactly three sentences what causes ocean tides.",
    "Outline in exactly three sentences why bread dough rises.",
    "State in exactly three sentences how noise-cancelling headphones work.",
    "Give in exactly three sentences the reason metals conduct electricity.",
    "Explain in exactly three sentences why the sky appears blue.",
    "Describe in exactly three sentences how a refrigerator moves heat.",
]


@dataclass
class Sample:
    ok: bool
    completion_tokens: int = 0
    latency_s: float = 0.0
    error: str | None = None
    rate_limited: bool = False

    @property
    def tok_s(self) -> float:
        return self.completion_tokens / self.latency_s if self.latency_s > 0 else 0.0


@dataclass
class Level:
    concurrency: int
    samples: list[Sample] = field(default_factory=list)
    wall_clock_s: float = 0.0

    @property
    def ok_samples(self) -> list[Sample]:
        return [s for s in self.samples if s.ok]

    @property
    def rate_limited(self) -> int:
        return sum(1 for s in self.samples if s.rate_limited)

    @property
    def total_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.ok_samples)

    @property
    def aggregate_tok_s(self) -> float:
        return self.total_tokens / self.wall_clock_s if self.wall_clock_s > 0 else 0.0

    @property
    def per_stream_tok_s(self) -> float:
        rates = [s.tok_s for s in self.ok_samples]
        return statistics.median(rates) if rates else 0.0


@dataclass
class BenchResult:
    role: str
    endpoint: str
    levels: list[Level] = field(default_factory=list)

    def baseline(self) -> Level | None:
        return next((lv for lv in self.levels if lv.concurrency == 1), None)

    def scaling(self, level: Level) -> float:
        """Aggregate throughput relative to a single stream. This is the number ADR-022 cares
        about: 3.62x means fan-out pays, 1.0x means requests serialise."""
        base = self.baseline()
        if not base or base.aggregate_tok_s == 0:
            return 0.0
        return level.aggregate_tok_s / base.aggregate_tok_s

    @property
    def usable(self) -> bool:
        return any(lv.ok_samples for lv in self.levels)

    def verdict(self) -> str:
        # Zero successful samples is the ABSENCE of a measurement, not a slow one. Reporting
        # "SERIALISES (0.00x)" after a wall of 403s would be a confident wrong reading —
        # exactly the failure mode the tool-fidelity work exists to prevent.
        if not self.usable:
            errors = [s.error for lv in self.levels for s in lv.samples if s.error]
            first = errors[0][:160] if errors else "unknown"
            return (
                f"NO MEASUREMENT — every request failed, so nothing about this model's "
                f"speed or scaling can be concluded. First error: {first}"
            )

        top = max((lv for lv in self.levels), key=lambda lv: lv.concurrency, default=None)
        if not top or top.concurrency == 1:
            return "no concurrency measured"
        if not top.ok_samples:
            return f"NO MEASUREMENT at concurrency {top.concurrency} — all requests failed"
        s = self.scaling(top)
        if s >= 2.0:
            return (
                f"SCALES ({s:.2f}x at {top.concurrency}) — fan-out pays; workers on this "
                f"model can run in parallel"
            )
        if s >= 1.3:
            return f"partial scaling ({s:.2f}x at {top.concurrency}) — modest benefit"
        return (
            f"SERIALISES ({s:.2f}x at {top.concurrency}) — concurrent requests buy nothing. "
            f"Do not fan out against this model."
        )


def _one(prompt: str, role: str, max_tokens: int) -> Sample:
    started = time.monotonic()
    try:
        result = llm.chat(
            [{"role": "user", "content": prompt}], role=role, max_tokens=max_tokens
        )
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        return Sample(
            ok=False,
            latency_s=time.monotonic() - started,
            error=text,
            rate_limited="429" in text or "rate" in text.lower(),
        )

    elapsed = time.monotonic() - started
    tokens = result.completion_tokens or 0
    if not tokens:
        # Without usage we cannot compute tok/s honestly; estimate and say so upstream.
        tokens = max(1, len(result.text) // 4)
    return Sample(ok=True, completion_tokens=tokens, latency_s=elapsed)


def run(
    role: str = "supervisor",
    concurrencies: tuple[int, ...] = (1, 4),
    max_tokens: int = 300,
    repeats: int = 1,
) -> BenchResult:
    from .config import get_models

    endpoint = get_models().role(role).endpoint
    result = BenchResult(role=role, endpoint=endpoint)

    for n in concurrencies:
        if n > len(PROMPTS):
            raise ValueError(f"only {len(PROMPTS)} distinct prompts available, asked for {n}")
        level = Level(concurrency=n)
        for r in range(repeats):
            # Rotate the prompt window between repeats so a warm KV cache from the previous
            # round cannot flatter the next one.
            offset = (r * n) % len(PROMPTS)
            batch = [PROMPTS[(offset + i) % len(PROMPTS)] for i in range(n)]

            started = time.monotonic()
            if n == 1:
                samples = [_one(batch[0], role, max_tokens)]
            else:
                with ThreadPoolExecutor(max_workers=n) as pool:
                    samples = list(pool.map(lambda p: _one(p, role, max_tokens), batch))
            level.wall_clock_s += time.monotonic() - started
            level.samples.extend(samples)

        log.info(
            "concurrency %d: %.1f tok/s aggregate, %.1f per stream, %d rate-limited",
            n,
            level.aggregate_tok_s,
            level.per_stream_tok_s,
            level.rate_limited,
        )
        result.levels.append(level)

    return result
