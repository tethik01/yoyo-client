"""Benchmark tests.

The bench exists to settle an empirical question, so its own arithmetic has to be right —
a scaling number that flatters a model would send the whole model decision the wrong way.
"""

import pytest

from yoyo import bench


def test_prompts_are_distinct():
    """Identical prompts let prefix caching serve later requests from the first one's KV
    cache. That looks like superb scaling and measures nothing."""
    assert len(set(bench.PROMPTS)) == len(bench.PROMPTS)
    assert len(bench.PROMPTS) >= 8


def _level(concurrency, tokens_each, wall):
    lv = bench.Level(concurrency=concurrency, wall_clock_s=wall)
    lv.samples = [
        bench.Sample(ok=True, completion_tokens=tokens_each, latency_s=wall)
        for _ in range(concurrency)
    ]
    return lv


def test_perfect_scaling_reads_as_scaling():
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(1, 100, 10.0), _level(4, 100, 10.0)]
    assert r.scaling(r.levels[1]) == pytest.approx(4.0)
    assert "SCALES" in r.verdict()


def test_full_serialisation_reads_as_serialising():
    """Four requests taking four times as long is 1.0x — the qwen3.6 case."""
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(1, 100, 10.0), _level(4, 100, 40.0)]
    assert r.scaling(r.levels[1]) == pytest.approx(1.0)
    assert "SERIALISES" in r.verdict()
    assert "Do not fan out" in r.verdict()


def test_partial_scaling_is_reported_as_such():
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(1, 100, 10.0), _level(4, 100, 25.0)]
    assert "partial" in r.verdict()


def test_the_measured_agent_figure_reproduces():
    """muse-glimmer: 16.7s -> 18.5s for 1 -> 4 requests, reported as 3.62x."""
    r = bench.BenchResult(role="x", endpoint="agent")
    r.levels = [_level(1, 200, 16.7), _level(4, 200, 18.5)]
    assert r.scaling(r.levels[1]) == pytest.approx(3.61, abs=0.05)
    assert "SCALES" in r.verdict()


def test_rate_limited_samples_are_counted_separately():
    """A per-key cap looks exactly like poor model scaling unless it is called out."""
    lv = bench.Level(concurrency=4, wall_clock_s=10.0)
    lv.samples = [
        bench.Sample(ok=True, completion_tokens=100, latency_s=10.0),
        bench.Sample(ok=False, error="429 rate limit", rate_limited=True),
    ]
    assert lv.rate_limited == 1
    assert lv.total_tokens == 100


def test_failed_samples_do_not_inflate_throughput():
    lv = bench.Level(concurrency=2, wall_clock_s=10.0)
    lv.samples = [
        bench.Sample(ok=True, completion_tokens=100, latency_s=10.0),
        bench.Sample(ok=False, error="boom"),
    ]
    assert lv.aggregate_tok_s == pytest.approx(10.0)


def test_asking_for_more_concurrency_than_prompts_is_refused():
    with pytest.raises(ValueError, match="distinct prompts"):
        bench.run("supervisor", concurrencies=(99,))


def test_no_baseline_means_no_scaling_claim():
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(4, 100, 10.0)]
    assert r.scaling(r.levels[0]) == 0.0


# ------------------------------------------------- absence of measurement ----
# Observed live: every request 403'd and the bench reported "SERIALISES (0.00x)". A wall of
# auth failures is not a model characteristic, and a confident wrong reading is worse than
# no reading.


def _failed_level(concurrency, error="403 key_model_access_denied"):
    lv = bench.Level(concurrency=concurrency, wall_clock_s=0.5)
    lv.samples = [bench.Sample(ok=False, error=error) for _ in range(concurrency)]
    return lv


def test_all_requests_failing_is_not_a_scaling_verdict():
    r = bench.BenchResult(role="coder_supervisor", endpoint="coder")
    r.levels = [_failed_level(1), _failed_level(4)]
    verdict = r.verdict()
    assert "NO MEASUREMENT" in verdict
    assert "SERIALISES" not in verdict
    assert not r.usable


def test_the_failure_reason_is_surfaced_in_the_verdict():
    r = bench.BenchResult(role="x", endpoint="coder")
    r.levels = [_failed_level(1, "403 key not allowed to access model")]
    assert "key not allowed" in r.verdict()


def test_partial_failure_still_measures_what_succeeded():
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(1, 100, 10.0), _failed_level(4)]
    assert r.usable
    assert "NO MEASUREMENT at concurrency 4" in r.verdict()


def test_usable_is_false_only_when_nothing_succeeded():
    r = bench.BenchResult(role="x", endpoint="y")
    r.levels = [_level(1, 100, 10.0)]
    assert r.usable
