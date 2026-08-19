"""Research: a topic in, a cited report out.

The value of a research report and its danger are the same property — it looks like
scholarship. A page of confident prose with a references section is exactly the shape people
stop checking, which makes this the highest-stakes place in the system for an invented
source. So most of these tests are about provenance and about failure, not about prose.

Everything is mocked at `websearch` and `llm`, so they run offline. What they prove: the
wiring, the provenance rule, and that a dead search degrades instead of exploding. What they
do not prove: that the reports are any good. That needs real topics and a reader.
"""

from __future__ import annotations

import pytest

from yoyo import research


@pytest.fixture()
def web(monkeypatch):
    """A search engine with two results, both fetchable."""
    from yoyo.websearch import Page, Result

    pages = {
        "https://example.com/a": Page(url="https://example.com/a", title="Alpha",
                                      text="Concurrency scales 3.75x on this hardware."),
        "https://example.com/b": Page(url="https://example.com/b", title="Beta",
                                      text="Others report serialisation instead."),
    }
    monkeypatch.setattr(research.websearch, "search", lambda q, limit=6: [
        Result(title="Alpha", url="https://example.com/a", snippet="scales"),
        Result(title="Beta", url="https://example.com/b", snippet="serialises"),
    ])
    monkeypatch.setattr(research.websearch, "fetch", lambda url, config=None: pages[url])
    return pages


def written(monkeypatch, text: str):
    from yoyo import llm

    monkeypatch.setattr(research, "plan", lambda topic, questions=4, role="extract":
                        research.Plan(questions=[f"what is {topic}"]))
    monkeypatch.setattr(llm, "chat", lambda messages, role="summarize", **kw: llm.ChatResult(
        text=text, model="test", role=role, endpoint="coder", reasoning=None,
        prompt_tokens=None, completion_tokens=None, tool_calls=None))


# ------------------------------------------------------------------ the run ---


def test_a_report_is_produced_with_its_sources(web, monkeypatch):
    written(monkeypatch, "Concurrency scales, per https://example.com/a.")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    assert "Concurrency scales" in report.text
    assert "## Sources" in report.text
    assert "https://example.com/a" in report.text


def test_pages_are_actually_read_not_just_snippets(web, monkeypatch):
    """A snippet is one sentence a search engine chose FOR apparent relevance — selected to
    match the query rather than to be true or representative. The worst possible evidence."""
    written(monkeypatch, "ok")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    assert report.read_count >= 1
    assert any(s.fetched for s in report.sources)


def test_a_page_that_cannot_be_fetched_survives_as_a_snippet(monkeypatch):
    from yoyo.websearch import Result

    monkeypatch.setattr(research.websearch, "search", lambda q, limit=6: [
        Result(title="Alpha", url="https://example.com/a", snippet="a useful sentence")])

    def refuse(url, config=None):
        raise research.websearch.SearchError("403 from the site")

    monkeypatch.setattr(research.websearch, "fetch", refuse)
    written(monkeypatch, "ok")

    report = research.run("anything", depth="quick", use_corpus=False)
    assert report.errors, "the failure must be recorded, not swallowed"
    assert report.sources and not report.sources[0].fetched


def test_a_dead_search_degrades_rather_than_exploding(monkeypatch):
    def boom(q, limit=6):
        raise research.websearch.SearchError("SearXNG is not running")

    monkeypatch.setattr(research.websearch, "search", boom)
    written(monkeypatch, "ignored")
    report = research.run("anything", depth="quick", use_corpus=False)
    assert report.errors
    assert "nothing to report" in report.text.lower()
    assert "failure to gather" in report.text.lower(), (
        "an empty report must say it failed to GATHER, not imply the topic was empty"
    )


def test_an_empty_topic_is_refused():
    with pytest.raises(research.ResearchError):
        research.run("   ")


def test_an_unknown_depth_is_refused():
    with pytest.raises(research.ResearchError):
        research.run("something", depth="exhaustive")


def test_a_failed_plan_falls_back_to_the_topic(monkeypatch):
    """Losing the whole run to a formatting error would throw away work the owner asked
    for. The topic itself is a serviceable single question."""
    monkeypatch.setattr("yoyo.structured.generate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad json")))
    plan = research.plan("quantisation trade-offs")
    assert plan.questions == ["quantisation trade-offs"]


# -------------------------------------------------------------- provenance ---


def test_an_invented_url_is_stripped_and_reported(web, monkeypatch):
    """THE test. A references section is where people stop checking, so a link that no tool
    returned must not survive to be printed."""
    written(monkeypatch, "See https://example.com/a and also https://totally-made-up.invalid/x")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    assert "totally-made-up.invalid" not in report.text
    assert report.invented_links
    assert "https://example.com/a" in report.text


def test_the_sources_section_is_built_from_what_was_read(web, monkeypatch):
    """Assembled by code, not written by the model — so the reference list cannot contain
    anything that was not retrieved, whatever the prose does."""
    written(monkeypatch, "A report with no links in the body at all.")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    section = report.text.split("## Sources")[1]
    for source in report.sources:
        assert source.url in section


def test_sources_only_glanced_at_are_labelled(web, monkeypatch):
    written(monkeypatch, "ok")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    if any(not s.fetched for s in report.sources):
        assert "snippet only" in report.text


# ------------------------------------------------------------ context budget ---


def test_sources_are_interleaved_across_questions_when_truncating():
    """Filling the context with question one's pages would leave later questions
    unanswered — and an unanswered section reads like the topic had nothing to say."""
    findings = [
        research.Findings(question="q1", sources=[
            research.Source(url=f"https://a/{i}", title="a", text="x" * 500, question="q1")
            for i in range(5)]),
        research.Findings(question="q2", sources=[
            research.Source(url=f"https://b/{i}", title="b", text="x" * 500, question="q2")
            for i in range(5)]),
    ]
    _block, used = research.render_sources(findings, budget=1600)
    assert any(s.url.startswith("https://b/") for s in used), (
        "the second question's sources were entirely crowded out"
    )


def test_the_writer_is_told_which_sources_were_only_glanced_at():
    findings = [research.Findings(question="q", sources=[
        research.Source(url="https://a", title="a", text="body", question="q", fetched=True),
        research.Source(url="https://b", title="b", text="snip", question="q", fetched=False),
    ])]
    block, _used = research.render_sources(findings)
    assert "(snippet only)" in block
    assert block.count("<source") == 2


# ---------------------------------------------------------------- the draft ---


def test_a_report_is_saved_as_a_draft_never_into_the_vault_proper(tmp_path, monkeypatch, web):
    """Same asymmetry as everywhere else: Yoyo drafts, the human decides what becomes canon.
    Drafts are excluded from search, so a report cannot come back as a source Yoyo cites at
    you as though you had written it."""
    from yoyo import vault

    monkeypatch.setattr(vault, "vault_root", lambda: tmp_path)
    written(monkeypatch, "A short report.")
    report = research.run("gpu concurrency", depth="quick", use_corpus=False)
    path = research.save_draft(report)

    assert path.startswith(vault.DRAFTS_DIR)
    saved = (tmp_path / path).read_text(encoding="utf-8")
    assert "generated_by: yoyo-research" in saved
    assert "A short report." in saved


def test_slugs_survive_a_messy_topic():
    assert research.slug("What's the *best* GPU?! ") == "what-s-the-best-gpu"
    assert research.slug("") == "research"


# ------------------------------------------------------------------- limits ---


def test_depth_controls_how_much_is_read():
    assert research.DEPTHS["quick"]["read"] < research.DEPTHS["deep"]["read"]
    assert research.DEPTHS["quick"]["questions"] < research.DEPTHS["deep"]["questions"]


def test_research_never_writes_to_the_vault_proper():
    """Structural: the only vault call in this module is the drafts-only one."""
    import inspect

    source = inspect.getsource(research)
    assert "write_draft" in source
    for forbidden in ("write_note", "vault.write(", "_resolve("):
        assert forbidden not in source
