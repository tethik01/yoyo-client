"""Deep research: a topic in, a cited report out.

`yoyo agent` answers a question with a handful of tool calls. Research is a different shape
of work — decompose, gather widely, read properly, then write something you could hand to
someone else. It takes minutes, it reads a dozen pages, and the output is a document rather
than a paragraph.

Four stages, and the interesting decisions are in the middle two:

    plan  ->  gather  ->  read  ->  write

**Plan.** One structured call turns a topic into sub-questions. Fewer, broader questions beat
many narrow ones: each costs a search and several fetches, and two questions that differ by a
synonym return the same pages twice.

**Gather runs in parallel, and that is new.** `coder` was recorded as serialising (1.09x) and
re-measured at 3.75x on 2026-08-15, which is exactly what makes a fan-out design worth
building today and would have been pointless last week. Sub-questions are independent, so
they fan out.

**Every source is real or it is not cited.** The provenance rule from `citations.py` applies
with full force here: a URL may appear in the report only if a tool put it there. A research
report is the highest-stakes place in this system for an invented source, because it *looks*
like scholarship — a page of confident prose with a references section is exactly the shape
people stop checking.

**Fetched pages are untrusted input.** `websearch.Page.as_dict` wraps them in the untrusted
marker and the SSRF gate has already run; nothing here strips an injection attempt, because
framing beats sanitising — a page that tries to give instructions should be visible doing it.

The report lands in `yoyo-drafts/research/`, never in the vault proper. Same asymmetry as
everywhere else: Yoyo drafts, you decide what becomes canon.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from . import citations, websearch

log = logging.getLogger(__name__)

#: How many sub-questions to answer. Beyond this the marginal question is usually a rephrase
#: of an earlier one, and it still costs a search and three fetches.
DEPTHS = {
    "quick":    {"questions": 2, "results": 4, "read": 2},
    "standard": {"questions": 4, "results": 6, "read": 3},
    "deep":     {"questions": 6, "results": 8, "read": 4},
}
DEFAULT_DEPTH = "standard"

#: Characters of each page handed to the writer. The context ceiling is 32K hard (and the
#: reasoning trace counts against it), so a dozen full pages would silently truncate the
#: prompt — losing the last sources rather than the least useful ones.
PAGE_BUDGET = 3_000


class ResearchError(RuntimeError):
    pass


# ------------------------------------------------------------------ planning ---


class Plan(BaseModel):
    questions: list[str] = Field(
        default_factory=list,
        description="Independent sub-questions. Each should need a different search.",
    )
    rationale: str = Field(default="", description="One line: why these, in this order")


PLAN_PROMPT = """Break this research topic into independent sub-questions.

Rules:
1. AT MOST {n} questions. Fewer is better — each one costs a web search and several page
   reads, and two questions that differ only by a synonym return the same pages twice.
2. Each question must need a DIFFERENT search. "What is X" and "explain X" are one question.
3. Cover the topic's distinct angles: what it is, how it works, what it costs, what the
   trade-offs are, who disagrees — whichever of those the topic actually has.
4. Write them as search-shaped questions, not essay titles.

TOPIC: {topic}"""


def plan(topic: str, questions: int = 4, role: str = "extract") -> Plan:
    from . import structured

    try:
        result = structured.generate(
            Plan, PLAN_PROMPT.format(n=questions, topic=topic), role=role
        )
    except Exception as exc:  # noqa: BLE001
        # A failed plan is not a failed research run: the topic itself is a serviceable
        # single question, and returning nothing here would throw away work the user asked
        # for over a formatting error.
        log.warning("planning failed (%s) — researching the topic as one question", exc)
        return Plan(questions=[topic], rationale="planning failed; using the topic verbatim")

    cleaned = [q.strip() for q in result.questions if q and q.strip()][:questions]
    return Plan(questions=cleaned or [topic], rationale=result.rationale)


# ----------------------------------------------------------------- gathering ---


@dataclass
class Source:
    """One thing that was actually read, and where it came from."""

    url: str
    title: str
    text: str
    question: str
    kind: str = "web"          # web | corpus | vault
    fetched: bool = False      # False = we only have the search snippet

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title, "kind": self.kind,
                "question": self.question, "fetched": self.fetched,
                "chars": len(self.text)}


@dataclass
class Findings:
    question: str
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def gather(question: str, results: int = 6, read: int = 3,
           on_log=None) -> Findings:  # noqa: ANN001
    """Search, then actually read the top few. Failures are recorded, never fatal.

    Reading matters: a snippet is one sentence chosen by a search engine to match a query,
    which is the worst possible evidence — it is selected FOR apparent relevance rather than
    for being true or representative. Pages that cannot be fetched stay as snippets and are
    marked `fetched: False`, so the writer can see which sources it only glanced at.
    """
    say = on_log or (lambda line: log.info("%s", line))
    found = Findings(question=question)

    try:
        hits = websearch.search(question, limit=results)
    except Exception as exc:  # noqa: BLE001 - one dead search must not kill the run
        found.errors.append(f"search failed: {exc}")
        say(f"  search failed for {question!r}: {exc}")
        return found

    say(f"  {len(hits)} result(s) for {question!r}")
    for hit in hits[:read]:
        try:
            page = websearch.fetch(hit.url)
            found.sources.append(Source(url=hit.url, title=page.title or hit.title,
                                        text=page.text[:PAGE_BUDGET], question=question,
                                        fetched=True))
        except Exception as exc:  # noqa: BLE001
            found.errors.append(f"{hit.url}: {exc}")
            found.sources.append(Source(url=hit.url, title=hit.title, text=hit.snippet,
                                        question=question, fetched=False))
    # The rest stay as snippets — cheap breadth next to the pages actually read.
    for hit in hits[read:]:
        found.sources.append(Source(url=hit.url, title=hit.title, text=hit.snippet,
                                    question=question, fetched=False))
    return found


def gather_local(question: str, top_k: int = 4) -> Findings:
    """What the owner's own corpus already says. Free, and often better than the web.

    Runs first in the report for a reason: a research report that ignores the documents you
    already have is a report you have to reconcile with them yourself.
    """
    found = Findings(question=question)
    try:
        from .rag import retrieve as rag

        for passage in rag.retrieve(question, top_k=top_k):
            found.sources.append(Source(
                url=f"[{passage.chunk_id}]", title=passage.title or passage.source_path,
                text=passage.text, question=question, kind="corpus", fetched=True))
    except Exception as exc:  # noqa: BLE001 - no corpus is a normal state, not an error
        log.info("corpus lookup skipped: %s", exc)
    return found


# ------------------------------------------------------------------- writing ---

WRITE_PROMPT = """Write a research report on the topic below, using ONLY the sources given.

Structure:
  # <topic>
  A two or three sentence answer to the topic as a whole. No preamble.

  ## <one section per sub-question>
  What the sources say. Cite every claim.

  ## What is unclear
  Where sources disagree, where evidence was thin, and what a reader should verify. Say
  "the sources did not cover X" plainly when they did not — an unanswered part of the topic
  is a finding, not something to paper over.

Rules — these are correctness requirements, not style preferences:

1. CITE with the exact identifier given in each source block: a URL for a web source, or
   `[12]` for a corpus chunk. Copy them character for character.
2. NEVER write a URL that is not in the sources below. Not as an example, not as "see also",
   not a homepage you inferred from a subpage. Invented links are stripped before you see
   the result, and the report says so — a references section is exactly the place people
   stop checking.
3. Sources marked `(snippet only)` were not read in full. Do not build a claim on one.
4. Where sources conflict, say so and cite both. Do not pick a winner silently.
5. No invented statistics, dates or quotations. If a number is not in a source, it does not
   go in the report.

TOPIC: {topic}

SUB-QUESTIONS:
{questions}

SOURCES:
{sources}"""


def render_sources(findings: list[Findings], budget: int = 24_000) -> tuple[str, list[Source]]:
    """The source block for the writer, and the sources that survived the budget.

    Truncation is round-robin across questions rather than sequential, because filling the
    context with question one's pages would leave the later questions unanswered — and an
    unanswered section reads like the topic had nothing to say about it.
    """
    per_question = [list(f.sources) for f in findings]
    ordered: list[Source] = []
    while any(per_question):
        for bucket in per_question:
            if bucket:
                ordered.append(bucket.pop(0))

    blocks: list[str] = []
    used: list[Source] = []
    total = 0
    for source in ordered:
        marker = "" if source.fetched else " (snippet only)"
        body = source.text.strip()
        block = (f"<source id=\"{source.url}\" title=\"{source.title}\"{marker}>\n"
                 f"{body}\n</source>")
        if total + len(block) > budget:
            continue
        blocks.append(block)
        used.append(source)
        total += len(block)
    return "\n\n".join(blocks), used


@dataclass
class ResearchReport:
    topic: str
    depth: str
    questions: list[str] = field(default_factory=list)
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    invented_links: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latency_ms: int = 0
    draft_path: str | None = None

    @property
    def read_count(self) -> int:
        return sum(1 for s in self.sources if s.fetched and s.kind == "web")

    def summary(self) -> str:
        return (f"{len(self.questions)} question(s) · {len(self.sources)} source(s) "
                f"({self.read_count} read in full) · {self.latency_ms // 1000}s"
                + (f" · {len(self.invented_links)} invented link(s) removed"
                   if self.invented_links else ""))


def write(topic: str, findings: list[Findings], role: str = "summarize") -> tuple[str, list[Source]]:
    from . import llm

    source_block, used = render_sources(findings)
    if not source_block.strip():
        return ("# " + topic + "\n\nNo sources were retrieved, so there is nothing to report. "
                "This is a failure to gather, not a finding about the topic.\n"), []

    questions = "\n".join(f"- {f.question}" for f in findings)
    result = llm.chat(
        [
            {"role": "system", "content":
             "You are a careful research assistant. You cite everything and you never "
             "invent a source. An unanswered question is a finding you report, not a gap "
             "you fill."},
            {"role": "user", "content": WRITE_PROMPT.format(
                topic=topic, questions=questions, sources=source_block)},
        ],
        role=role,
    )
    return result.text or "", used


# ----------------------------------------------------------------------- run ---


def run(topic: str, depth: str = DEFAULT_DEPTH, use_corpus: bool = True,
        on_log=None) -> ResearchReport:  # noqa: ANN001
    """Plan, gather, read, write. Minutes of work, one document out."""
    say = on_log or (lambda line: log.info("%s", line))
    if not (topic or "").strip():
        raise ResearchError("nothing to research")
    if depth not in DEPTHS:
        raise ResearchError(f"depth must be one of {', '.join(DEPTHS)}")

    started = time.monotonic()
    knobs = DEPTHS[depth]
    report = ResearchReport(topic=topic.strip(), depth=depth)

    say(f"planning ({depth}) …")
    the_plan = plan(topic, questions=knobs["questions"])
    report.questions = the_plan.questions
    for question in the_plan.questions:
        say(f"  · {question}")

    findings: list[Findings] = []
    if use_corpus:
        local = gather_local(topic)
        if local.sources:
            say(f"{len(local.sources)} passage(s) from your own corpus")
            findings.append(local)

    # Fan out. Independent questions, and `coder` scales 3.75x at four parallel — measured
    # 2026-08-15, after an earlier single-trial reading of 1.09x said the opposite. This
    # design would have been pointless under the old number.
    say(f"searching {len(the_plan.questions)} question(s) in parallel …")
    with ThreadPoolExecutor(max_workers=min(4, len(the_plan.questions))) as pool:
        futures = [
            pool.submit(gather, q, knobs["results"], knobs["read"], say)
            for q in the_plan.questions
        ]
        for future in futures:
            found = future.result()
            findings.append(found)
            report.errors.extend(found.errors)

    total = sum(len(f.sources) for f in findings)
    say(f"{total} source(s) gathered; writing …")
    text, used = write(topic, findings)
    report.sources = used

    # Provenance, mechanically. A report is the highest-stakes place in this system for an
    # invented source: a page of confident prose with a references section is exactly the
    # shape people stop checking.
    allowed = "\n".join(s.url for s in used)
    text, unsupported = citations.strip_unsupported_urls(text, allowed)
    text, fabricated = citations.strip_fabricated_links(text)
    report.invented_links = sorted(set(unsupported) | set(fabricated))
    if report.invented_links:
        say(f"removed {len(report.invented_links)} invented link(s): "
            f"{', '.join(report.invented_links[:3])}")

    report.text = text + _sources_section(used)
    report.latency_ms = int((time.monotonic() - started) * 1000)
    say(report.summary())
    return report


def _sources_section(sources: list[Source]) -> str:
    """Appended by code, not written by the model.

    The model cites inline; the list at the bottom is assembled from what was actually
    retrieved. That way the references section cannot contain anything that was not read,
    whatever the prose does.
    """
    if not sources:
        return ""
    lines = ["", "", "## Sources", ""]
    for source in sources:
        mark = "" if source.fetched else " *(snippet only — not read in full)*"
        if source.kind == "corpus":
            lines.append(f"- `{source.url}` {source.title} — your corpus{mark}")
        else:
            lines.append(f"- {source.url} — {source.title}{mark}")
    return "\n".join(lines) + "\n"


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(topic: str) -> str:
    return _SLUG.sub("-", (topic or "").lower()).strip("-")[:60] or "research"


def save_draft(report: ResearchReport) -> str:
    """Into `yoyo-drafts/research/`, never the vault proper.

    Same asymmetry as everywhere else: Yoyo drafts, the human decides what becomes canon.
    Drafts are excluded from search, so a report cannot become a source Yoyo later cites
    back at you as though you had written it.
    """
    from datetime import UTC, datetime

    from . import vault

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    header = (f"---\ntopic: {report.topic}\ngenerated_by: yoyo-research\n"
              f"depth: {report.depth}\ndate: {stamp}\n"
              f"sources: {len(report.sources)}\n---\n\n")
    # Flat name, not a subfolder: `write_draft` deliberately discards any directory
    # component so a caller cannot escape the drafts folder, and a prefix reads the same in
    # a file list anyway.
    written = vault.write_draft(f"research-{stamp}-{slug(report.topic)}.md",
                                header + report.text, overwrite=True)
    report.draft_path = written["path"]
    return written["path"]
