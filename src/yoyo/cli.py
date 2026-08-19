"""Yoyo CLI.  `yoyo --help`"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import agent as agent_mod
from . import backup as backup_mod
from . import bench as bench_mod
from . import calendar as cal_mod
from . import core, doctor, router
from . import evals as evals_mod
from . import mail as mail_mod
from . import tasks as tasks_mod
from . import tools as tools_mod
from . import voice as voice_mod
from . import websearch as web_mod
from .config import get_settings
from .graph import GraphBudget
from .graph import run as graph_run
from .mcp import client as mcp_client
from .rag import ingest as ingest_mod
from .rag import retrieve as rag
from .storage import db, vectors
from .voice import speech as voice_speech

app = typer.Typer(help="Yoyo — local assistant. Inference runs on MyAIServer.", no_args_is_help=True)
console = Console()


def _require_served(role: str) -> None:
    """Fail fast when a role's capability is not reachable.

    Observed live: judging a candidate model whose capability the key could not access
    burned all seven eval cases on identical 403s. One clear message beats seven copies of
    the same one.
    """
    from . import llm
    from .config import get_models

    # A mistyped or renamed role is a user error, not a crash. Print the known roles.
    try:
        endpoint = get_models().role(role).endpoint
    except KeyError as exc:
        console.print(f"[red]{exc.args[0]}[/]")
        raise typer.Exit(2) from None
    except ValueError as exc:  # tools:true pointed at a non-tool-reliable capability
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None

    try:
        served = llm.list_models()
    except Exception as exc:  # noqa: BLE001 - reachability is doctor's job, not ours
        console.print(f"[yellow]could not list served models ({exc}); continuing[/]")
        return

    if endpoint not in served:
        console.print(
            f"[red]Role [bold]{role}[/bold] maps to capability [bold]{endpoint}[/bold], "
            f"which this key cannot reach.[/]\n"
            f"[dim]Served: {', '.join(served)}[/]\n\n"
            f"Either the model is not registered in LiteLLM, or this laptop's virtual key "
            f"is not permitted to use it. LiteLLM filters /v1/models by key permission, so "
            f"an unlisted model usually means the key, not the config.\n"
            f"Fix: add {endpoint!r} to the key's allowed models, then re-run `yoyo doctor`."
        )
        raise typer.Exit(2)


def _setup_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(levelname)-7s %(name)s: %(message)s",
    )


@app.command("doctor")
def doctor_cmd() -> None:
    """Check every seam: tailnet, auth, model names, embeddings, SQLite, Qdrant."""
    _setup_logging()
    checks = doctor.run_all()
    table = Table(show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("")
    table.add_column("detail", overflow="fold")
    for c in checks:
        table.add_row(c.name, "[green]PASS[/]" if c.ok else "[red]FAIL[/]", c.detail)
    console.print(table)
    raise typer.Exit(0 if all(c.ok for c in checks) else 1)


@app.command()
def migrate() -> None:
    """Apply pending SQLite migrations."""
    _setup_logging()
    applied = db.migrate()
    console.print(f"[green]applied[/]: {', '.join(applied) if applied else 'nothing (up to date)'}")


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, help="File or folder to ingest"),
    recursive: bool = typer.Option(True, help="Walk subdirectories"),
) -> None:
    """Ingest files into the corpus and embed them."""
    _setup_logging()
    report = ingest_mod.ingest_path(path, recursive=recursive)
    console.print(f"[green]{report.summary()}[/]")
    for src, err in report.failed:
        console.print(f"[yellow]failed[/] {src}: {err}")


@app.command()
def search(query: str, top_k: int = typer.Option(6)) -> None:
    """Retrieve passages without calling the model."""
    _setup_logging()
    for p in rag.retrieve(query, top_k=top_k):
        console.print(f"[bold]{escape(p.cite())}[/] score={p.score:.4f}")
        console.print(escape(p.text[:400].replace("\n", " ")) + "\n")


@app.command()
def ask(
    question: str,
    role: str = typer.Option("answer", help="Role from yoyo-models.yaml"),
    no_rag: bool = typer.Option(False, "--no-rag"),
    conversation: Optional[int] = typer.Option(None, help="Conversation id to append to"),  # noqa: UP045
) -> None:
    """Ask a question."""
    _setup_logging()
    answer = core.ask(
        question, conversation_id=conversation, role=role, use_rag=not no_rag
    )
    console.print(escape(answer.text))
    console.print(
        f"\n[dim]{answer.model} · {answer.latency_ms} ms · {len(answer.passages)} sources[/]"
    )
    for p in answer.passages:
        console.print(f"[dim]  {p.chunk_id} {escape(p.cite())}[/]")


@app.command()
def reindex(recreate: bool = typer.Option(False, help="Drop and rebuild the Qdrant collection")) -> None:
    """Re-embed everything. Required after changing the embed model."""
    _setup_logging()
    from . import embeddings

    vectors.ensure_collection(embeddings.dimensions(), recreate=recreate)
    with db.connection() as conn:
        if recreate:
            with db.transaction(conn):
                conn.execute("UPDATE chunks SET embedded_at = NULL, embed_model = NULL")
        n = ingest_mod.embed_pending(conn)
    console.print(f"[green]embedded {n} chunks[/]")


@app.command()
def stats() -> None:
    """Corpus and conversation counts."""
    _setup_logging()
    with db.connection() as conn:
        for k, v in db.stats(conn).items():
            console.print(f"{k:20} {v}")
    console.print(vectors.info())


@app.command()
def serve() -> None:
    """Run the local HTTP API."""
    _setup_logging()
    from .api import serve as _serve

    _serve()


@app.command()
def backup(
    dest: Path = typer.Argument(..., help="Folder on the external drive, e.g. E:\\yoyo-backups"),
) -> None:
    """Snapshot SQLite + config to a timestamped archive. Vectors are not included."""
    _setup_logging()
    archive = backup_mod.create(dest)
    size = archive.stat().st_size / 1024
    console.print(f"[green]wrote[/] {archive} ({size:.1f} KB)")
    console.print("[dim]Now verify it — an unverified backup is a guess:[/]")
    console.print(f"[dim]  yoyo restore-drill \"{archive}\"[/]")


@app.command("restore-drill")
def restore_drill(
    archive: Optional[Path] = typer.Argument(None, help="Archive to verify"),  # noqa: UP045
    dest: Optional[Path] = typer.Option(None, help="Folder to take the newest archive from"),  # noqa: UP045
) -> None:
    """Prove a backup can be restored. Reads only — never touches live data."""
    _setup_logging()
    if archive is None:
        if dest is None:
            raise typer.BadParameter("give an archive path, or --dest to use the newest one")
        archive = backup_mod.latest(dest)
        if archive is None:
            raise typer.BadParameter(f"no yoyo-backup-*.zip found in {dest}")

    result = backup_mod.restore_drill(archive)
    table = Table(show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("")
    table.add_column("detail", overflow="fold")
    for name, ok, detail in result.checks:
        table.add_row(name, "[green]PASS[/]" if ok else "[red]FAIL[/]", escape(detail))
    console.print(table)
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def restore(
    archive: Path = typer.Argument(..., exists=True),
    force: bool = typer.Option(False, help="Overwrite the existing database"),
) -> None:
    """Replace the live database from an archive, then re-embed."""
    _setup_logging()
    path = backup_mod.restore(archive, force=force)
    console.print(f"[green]restored[/] {path}")
    console.print("[yellow]Vectors were not restored. Run:[/] yoyo reindex --recreate")


@app.command()
def tools() -> None:
    """List registered tools."""
    _setup_logging()
    table = Table(show_header=True, header_style="bold")
    table.add_column("tool")
    table.add_column("description", overflow="fold")
    for name in tools_mod.registry.names():
        table.add_row(name, escape(tools_mod.registry.get(name).description))
    console.print(table)


@app.command()
def agent(
    question: str,
    role: str = typer.Option("supervisor", help="Must be a tools:true role"),
    max_iterations: int = typer.Option(8),
    mcp: bool = typer.Option(True, help="Mount MCP servers from yoyo-mcp.yaml"),
) -> None:
    """Run a tool-calling turn. Slower than `ask` — this is the `agent` capability."""
    _setup_logging()
    if mcp:
        for name, r in mcp_client.mount_all().items():
            if not r["ok"]:
                console.print(f"[yellow]mcp {name}: {escape(r['error'])}[/]")
    result = agent_mod.run(question, role=role, max_iterations=max_iterations)
    console.print(escape(result.text))
    if result.fabricated_links:
        # Shown, not hidden. The scrubber makes the answer safe to read; it does not make
        # the model trustworthy, and the owner should know which turns needed it.
        console.print(
            f"\n[yellow]warning: removed {len(result.fabricated_links)} fabricated citation "
            f"path(s) from this answer — the model invented "
            f"{escape(', '.join(result.fabricated_links))}[/]"
        )
    console.print(
        f"\n[dim]{result.model} · {result.latency_ms} ms · {result.iterations} iterations "
        f"· {result.stopped_because}[/]"
    )
    for inv in result.invocations:
        mark = "[green]ok[/]" if inv.ok else "[red]err[/]"
        console.print(f"[dim]  {mark} {escape(inv.name)}({escape(str(inv.arguments))})[/]")
        if not inv.ok:
            console.print(f"[dim]       {escape(inv.error or '')}[/]")


@app.command()
def do(
    question: str,
    mode: Optional[str] = typer.Option(  # noqa: UP045
        None, help="Force ask | agent | plan. Omit to let Yoyo choose."
    ),
    rules_only: bool = typer.Option(
        False, "--rules-only", help="Skip the classifier — deterministic routing only"
    ),
    conversation: Optional[int] = typer.Option(None, help="Continue a conversation"),  # noqa: UP045
    mcp: bool = typer.Option(True, help="Mount MCP servers from yoyo-mcp.yaml"),
) -> None:
    """Ask without picking a mode — Yoyo routes, says what it chose, and you can override.

    The choice is printed before the answer, every time. Routing that you cannot see is
    routing you cannot correct, and the mode choice was doing real safety work: `ask` has no
    tools and will answer a question about your mail anyway.
    """
    _setup_logging()
    if mcp:
        for name, r in mcp_client.mount_all().items():
            if not r["ok"]:
                console.print(f"[yellow]mcp {name}: {escape(r['error'])}[/]")

    try:
        answer = router.run(question, override=mode, conversation_id=conversation,
                            use_model=not rules_only)
    except router.RouteError as exc:
        raise typer.BadParameter(str(exc)) from exc

    decision = answer.route
    console.print(f"[cyan]▸ {decision.mode}[/] [dim]— {escape(decision.reason)}[/]")
    if decision.decided_by in {"rules", "fallback"}:
        console.print(f"[dim]  (decided by {decision.decided_by})[/]")
    console.print()
    console.print(escape(answer.text))
    if answer.fabricated_links:
        console.print(
            f"\n[yellow]warning: removed {len(answer.fabricated_links)} fabricated "
            f"citation path(s)[/]"
        )
    console.print(f"\n[dim]{answer.model} · {answer.latency_ms} ms · {answer.detail}[/]")
    console.print("[dim]not what you wanted? re-run with --mode ask|agent|plan[/]")


@app.command()
def route(
    question: str,
    rules_only: bool = typer.Option(False, "--rules-only", help="Deterministic routing only"),
) -> None:
    """Show how a question would be routed, without answering it.

    Exists so routing is inspectable on its own. A misrouted answer and a bad answer look
    identical from the outside; this separates them.
    """
    _setup_logging()
    decision = router.route(question, use_model=not rules_only)
    table = Table(show_header=False, box=None)
    table.add_row("mode", f"[cyan]{decision.mode}[/]")
    table.add_row("reason", escape(decision.reason))
    table.add_row("decided by", decision.decided_by)
    table.add_row("rules floor", decision.floor)
    table.add_row("signals", escape(", ".join(decision.signals) or "none"))
    table.add_row("question", escape(decision.question))
    console.print(table)


@app.command()
def research(
    topic: str,
    depth: str = typer.Option("standard", help="quick | standard | deep"),
    corpus: bool = typer.Option(True, help="Also read your own documents"),
    save: bool = typer.Option(True, help="Save the report to yoyo-drafts/"),
) -> None:
    """Research a topic properly: plan, search, read the pages, write a cited report.

    Minutes, not seconds — several searches and a dozen page fetches. Every URL in the
    report was returned by a search or fetched; invented ones are stripped and the removal
    is reported, because a references section is exactly where people stop checking.
    """
    _setup_logging()
    from . import research as research_mod

    try:
        report = research_mod.run(topic, depth=depth, use_corpus=corpus,
                                  on_log=lambda line: console.print(f"[dim]{escape(line)}[/]"))
    except research_mod.ResearchError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print()
    console.print(escape(report.text))
    console.print(f"\n[dim]{report.summary()}[/]")
    if report.invented_links:
        console.print(f"[yellow]removed {len(report.invented_links)} invented link(s): "
                      f"{escape(', '.join(report.invented_links))}[/]")
    for err in report.errors[:5]:
        console.print(f"[dim]could not read: {escape(err)}[/]")
    if save:
        try:
            console.print(f"[green]saved draft:[/] {research_mod.save_draft(report)}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]could not save a draft: {escape(str(exc))}[/]")


@app.command()
def eval(
    only: Optional[str] = typer.Option(None, help="Case id or kind to run"),  # noqa: UP045
    role: Optional[str] = typer.Option(  # noqa: UP045
        None, help="Override the role for every case — use to judge a candidate model"
    ),
) -> None:
    """Run the golden evaluation set. These are gates, not a score."""
    _setup_logging()
    console.print(
        "[dim]Agent cases take 30-60 s each (thinking is on). Budget ~5 min for the full set.[/]\n"
    )

    def started(i, total, case):
        console.print(
            f"[dim]({i}/{total})[/] {case['id']} [dim]{case['kind']}[/] ...", end=""
        )

    def finished(i, total, r):
        mark = "[green]PASS[/]" if r.passed else "[red]FAIL[/]"
        console.print(f" {mark} [dim]{r.latency_ms / 1000:.1f}s[/]")
        if not r.passed:
            console.print(f"      [red]{escape(r.detail)}[/]")

    if role:
        from .config import get_models

        _require_served(role)
        endpoint = get_models().role(role).endpoint
        console.print(f"[bold]Judging role [cyan]{role}[/] -> capability [cyan]{endpoint}[/][/]\n")

    report = evals_mod.run(
        only=[only] if only else None,
        on_start=started,
        on_result=finished,
        role_override=role,
    )

    console.print()
    table = Table(show_header=True, header_style="bold")
    table.add_column("case")
    table.add_column("kind")
    table.add_column("")
    table.add_column("detail", overflow="fold")
    for r in report.results:
        table.add_row(
            r.case_id, r.kind, "[green]PASS[/]" if r.passed else "[red]FAIL[/]", escape(r.detail)
        )
    console.print(table)
    console.print(f"{report.passed}/{report.total} passed")
    if not report.ok:
        console.print("[red]Gates failed. Do not pin a model to the affected role.[/]")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def plan(
    question: str,
    max_subtasks: int = typer.Option(4, help="Ceiling on decomposition"),
    max_parallel: int = typer.Option(3, help="Concurrent workers (server has 4 slots, shared)"),
    mcp: bool = typer.Option(True, help="Mount MCP servers from yoyo-mcp.yaml"),
) -> None:
    """Multi-step research: plan, delegate to parallel workers, synthesise."""
    _setup_logging()
    if mcp:
        for name, r in mcp_client.mount_all().items():
            if not r["ok"]:
                console.print(f"[yellow]mcp {name}: {escape(r['error'])}[/]")

    budget = GraphBudget(max_subtasks=max_subtasks, max_parallel=max_parallel)
    result = graph_run(question, budget=budget)

    if result.plan and result.plan.subtasks:
        console.print("[bold]Plan[/]")
        if result.plan.reasoning:
            console.print(f"  [dim]{escape(result.plan.reasoning)}[/]")
        for st in result.plan.subtasks:
            console.print(f"  {st.id}. {escape(st.goal)}")
        console.print()

    console.print(escape(result.answer))
    console.print(
        f"\n[dim]{result.latency_ms / 1000:.1f}s · {result.subtask_count} subtasks "
        f"· {result.stopped_because}[/]"
    )
    for r in result.results:
        mark = "[green]ok[/]" if r.ok else "[red]fail[/]"
        tools = ", ".join(r.tools_used) or "no tools"
        console.print(
            f"[dim]  {mark} {r.subtask_id}. {escape(r.goal[:60])} "
            f"({r.latency_ms / 1000:.0f}s, {escape(tools)})[/]"
        )
    for n in result.notes:
        console.print(f"[yellow]  note: {escape(n)}[/]")


mcp_app = typer.Typer(help="MCP servers: mount external ones, or serve Yoyo's own.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("list")
def mcp_list() -> None:
    """Mount everything in yoyo-mcp.yaml and show what each server provides."""
    _setup_logging()
    specs = mcp_client.load_config()
    if not specs:
        console.print("[yellow]No servers configured in yoyo-mcp.yaml[/]")
        raise typer.Exit(0)

    report = mcp_client.mount_all()
    table = Table(show_header=True, header_style="bold")
    table.add_column("server")
    table.add_column("")
    table.add_column("tools", overflow="fold")
    for name, r in report.items():
        if r.get("skipped"):
            table.add_row(name, "[dim]off[/]", "[dim]disabled in yoyo-mcp.yaml[/]")
        elif r["ok"]:
            table.add_row(name, "[green]up[/]", escape(", ".join(r["tools"]) or "<none>"))
        else:
            table.add_row(name, "[red]fail[/]", escape(r["error"]))
    console.print(table)
    mcp_client.unmount_all()


@mcp_app.command("serve-vault")
def mcp_serve_vault() -> None:
    """Serve the Obsidian vault over MCP on stdio (for Claude Desktop, or Yoyo itself)."""
    from .mcp.vault_server import main

    main()


@mcp_app.command("serve-mail")
def mcp_serve_mail() -> None:
    """Serve mail (Gmail + Microsoft 365) over MCP on stdio. Read and draft only."""
    from .mcp.mail_server import main

    main()


@mcp_app.command("serve-tasks")
def mcp_serve_tasks() -> None:
    """Serve the vault's checkbox tasks over stdio."""
    from .mcp.tasks_server import main

    main()


@mcp_app.command("serve-calendar")
def mcp_serve_calendar() -> None:
    """Serve calendar events over stdio. Read-only."""
    from .mcp.calendar_server import main

    main()


@mcp_app.command("serve-search")
def mcp_serve_search() -> None:
    """Serve web search and fetching over stdio."""
    from .mcp.search_server import main

    main()


@mcp_app.command("serve-corpus")
def mcp_serve_corpus() -> None:
    """Serve the ingested corpus over MCP on stdio. Read-only."""
    from .mcp.corpus_server import main

    main()


@app.command()
def bench(
    role: str = typer.Option("supervisor", help="Role whose capability to measure"),
    concurrency: str = typer.Option("1,4", help="Comma-separated levels, e.g. 1,2,4"),
    max_tokens: int = typer.Option(300),
    repeats: int = typer.Option(1, help="Rounds per level; prompts rotate between rounds"),
) -> None:
    """Measure single-stream speed and concurrency scaling for one capability.

    Concurrency is empirical (ADR-022) — two architectural hypotheses were tested and both
    falsified. Never infer scaling from a model being MoE or from a sibling model.
    """
    _setup_logging()
    _require_served(role)
    levels = tuple(int(x) for x in concurrency.split(",") if x.strip())
    result = bench_mod.run(role, levels, max_tokens=max_tokens, repeats=repeats)

    table = Table(show_header=True, header_style="bold")
    table.add_column("concurrency", justify="right")
    table.add_column("aggregate tok/s", justify="right")
    table.add_column("per-stream tok/s", justify="right")
    table.add_column("scaling", justify="right")
    table.add_column("wall clock", justify="right")
    table.add_column("429s", justify="right")
    for lv in result.levels:
        table.add_row(
            str(lv.concurrency),
            f"{lv.aggregate_tok_s:.1f}",
            f"{lv.per_stream_tok_s:.1f}",
            f"{result.scaling(lv):.2f}x" if lv.concurrency > 1 else "-",
            f"{lv.wall_clock_s:.1f}s",
            str(lv.rate_limited) if lv.rate_limited else "-",
        )
    console.print(f"[bold]{result.role}[/] -> capability [cyan]{result.endpoint}[/]")
    console.print(table)
    if result.usable:
        console.print(result.verdict())
    else:
        console.print(f"[red]{result.verdict()}[/]")
    if any(lv.rate_limited for lv in result.levels):
        console.print(
            "[yellow]Some requests were rate limited. Per-key max_parallel_requests caps "
            "this laptop at ~2 — that is a policy limit, not the model serialising.[/]"
        )


mail_app = typer.Typer(help="Mail accounts: Gmail and Microsoft 365. Read and draft only.")
app.add_typer(mail_app, name="mail")


@mail_app.command("accounts")
def mail_accounts() -> None:
    """Configured accounts and whether each is authenticated."""
    _setup_logging()
    rows = mail_mod.status()
    if not rows:
        console.print("[yellow]No accounts in yoyo-mail.yaml[/]")
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold")
    table.add_column("account")
    table.add_column("provider")
    table.add_column("enabled")
    table.add_column("authenticated")
    table.add_column("note", overflow="fold")
    for r in rows:
        auth = r.get("authenticated")
        table.add_row(
            r["account"],
            r["provider"],
            "yes" if r["enabled"] else "[dim]no[/]",
            "[green]yes[/]" if auth else ("[red]no[/]" if r["enabled"] else "[dim]-[/]"),
            escape(r.get("error") or r.get("description") or ""),
        )
    console.print(table)
    console.print(f"[dim]tokens: {mail_mod.token_dir()}[/]")


@mail_app.command("auth")
def mail_auth(account: str) -> None:
    """Run the OAuth consent flow for one account. Opens a browser; Yoyo never sees your password."""
    _setup_logging()
    specs = {s.name: s for s in mail_mod.load_accounts()}
    if account not in specs:
        raise typer.BadParameter(
            f"unknown account {account!r}. Configured: {', '.join(sorted(specs)) or '<none>'}"
        )
    console.print(
        "[yellow]Note:[/] this stores a long-lived refresh token for the whole mailbox "
        f"under {mail_mod.token_dir()}. The disk is not encrypted (OQ4)."
    )
    provider = mail_mod.build(specs[account])
    console.print(f"[green]{provider.authenticate()}[/]")
    # Authenticating a disabled account succeeds and then every later command refuses to use
    # it. Observed live: auth said "authenticated personal (gmail)" and the very next search
    # said "no mail accounts configured". Say it here, at the moment it can be acted on.
    if not specs[account].enabled:
        console.print(
            f"[yellow]{account} is still `enabled: false` in yoyo-mail.yaml — "
            f"set it to true or nothing will use this token.[/]"
        )


@mail_app.command("search")
def mail_search(
    query: str,
    account: Optional[str] = typer.Option(None),  # noqa: UP045
    limit: int = typer.Option(10),
) -> None:
    """Search mail without involving a model."""
    _setup_logging()
    provider = mail_mod.resolve(account)
    for m in provider.search(query, limit=limit):
        when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
        flag = "[bold]*[/]" if m.unread else " "
        console.print(f"{flag} [dim]{when}[/] {escape(m.sender)}")
        console.print(f"   {escape(m.subject)}")
        # Gmail's snippet is often just the subject again (shipping notices, receipts).
        # Printing both doubles every result for no information.
        snippet = m.snippet.strip()
        if snippet and snippet[:60].lower() != m.subject.strip()[:60].lower():
            console.print(f"   [dim]{escape(snippet[:140])}[/]")
        console.print(f"   [dim]id={m.id}[/]\n")


voice_app = typer.Typer(help="Voice: engines, devices, and what actually works.")
app.add_typer(voice_app, name="voice")


@voice_app.command("status")
def voice_status() -> None:
    """What is configured for speech, and whether each piece actually works."""
    _setup_logging()
    table = Table(show_header=True, header_style="bold")
    table.add_column("component")
    table.add_column("engine")
    table.add_column("")
    table.add_column("detail", overflow="fold")
    for row in voice_mod.status():
        table.add_row(
            row["component"],
            row["engine"],
            "[green]ok[/]" if row["available"] else "[red]missing[/]",
            escape(row["detail"] or row["hint"] or ""),
        )
    console.print(table)
    console.print("[dim]All voice processing is local. No audio leaves this laptop.[/]")


@voice_app.command("devices")
def voice_devices() -> None:
    """List microphones. The index goes in yoyo-voice.yaml under mic.device."""
    _setup_logging()
    from .voice.mic import default_device, list_devices

    devices = list_devices()
    if not devices:
        console.print("[yellow]No input devices found.[/]")
        raise typer.Exit(1)
    chosen = default_device()
    table = Table(show_header=True, header_style="bold")
    table.add_column("index", justify="right")
    table.add_column("name", overflow="fold")
    table.add_column("channels", justify="right")
    for d in devices:
        mark = " [green](default)[/]" if chosen and d["index"] == chosen["index"] else ""
        table.add_row(str(d["index"]), escape(d["name"]) + mark, str(d["channels"]))
    console.print(table)


@app.command()
def transcribe(
    audio: Path = typer.Argument(..., exists=True, help="Audio file to transcribe"),
    model: Optional[str] = typer.Option(None, help="Override the whisper model size"),  # noqa: UP045
    language: Optional[str] = typer.Option(None, help='e.g. "en" — blank autodetects'),  # noqa: UP045
    out: Optional[Path] = typer.Option(None, help="Write the transcript here (.md)"),  # noqa: UP045
    ingest: bool = typer.Option(False, "--ingest", help="Also ingest it into the corpus"),
    timestamps: bool = typer.Option(True, help="Keep [HH:MM:SS] markers"),
) -> None:
    """Transcribe an audio file locally. Nothing is uploaded."""
    _setup_logging()
    if not voice_mod.looks_like_audio(str(audio)):
        console.print(f"[yellow]{audio.suffix} is not a known audio extension — trying anyway.[/]")

    cfg = voice_mod.load_config()
    engine = voice_mod.get_transcriber(cfg, model=model)
    if not engine.is_available():
        console.print('[red]faster-whisper is not installed.[/] uv pip install -e ".[voice]"')
        raise typer.Exit(1)

    console.print(f"[dim]transcribing {audio.name} — the first run downloads the model[/]")
    result = engine.transcribe(str(audio), language=language or cfg.stt_language)

    body = result.with_timestamps() if timestamps else result.text
    console.print(escape(body))
    console.print(
        f"\n[dim]{result.engine} · {result.model} · {result.duration_s:.0f}s audio in "
        f"{result.latency_ms / 1000:.1f}s ({result.realtime_factor:.1f}x realtime) · "
        f"lang={result.language}[/]"
    )
    if not result.text.strip():
        console.print("[yellow]Nothing was transcribed. Check the file has audible speech.[/]")
        raise typer.Exit(1)

    # A sidecar .md beside the audio, so ingest reuses the whole existing pipeline —
    # content hashing, chunking, embedding — rather than a second path that would drift.
    target = out or audio.with_suffix(".transcript.md")
    header = (
        f"# Transcript: {audio.name}\n\n"
        f"- source: `{audio.name}`\n"
        f"- duration: {voice_mod.format_timestamp(result.duration_s)}\n"
        f"- engine: {result.engine}, model {result.model}\n"
        f"- transcribed locally; audio never left this machine\n\n"
    )
    target.write_text(header + body + "\n", encoding="utf-8")
    console.print(f"[green]wrote[/] {target}")

    if ingest:
        report = ingest_mod.ingest_path(target, recursive=False)
        console.print(f"[green]{report.summary()}[/]")
        for src, err in report.failed:
            console.print(f"[yellow]failed[/] {src}: {err}")


@app.command()
def say(
    text: str,
    engine: Optional[str] = typer.Option(None, help="piper | sapi — overrides config"),  # noqa: UP045
    out: Optional[Path] = typer.Option(None, help="Write a .wav instead of playing it"),  # noqa: UP045
) -> None:
    """Speak text aloud, or render it to a wav."""
    _setup_logging()
    speaker = voice_mod.get_speaker(engine=engine)
    if not speaker.is_available():
        console.print(f"[red]The {speaker.name} voice is not usable.[/] Run `yoyo voice status`.")
        raise typer.Exit(1)
    if out:
        console.print(f"[green]wrote[/] {speaker.synthesise(text, str(out))}")
    else:
        speaker.speak(text)


@app.command()
def talk(
    mode: str = typer.Option(
        "auto", help="auto | ask | agent | plan — auto lets Yoyo route each turn"
    ),
    device: Optional[int] = typer.Option(None, help="Microphone index"),  # noqa: UP045
    speak_reply: bool = typer.Option(True, "--speak/--no-speak", help="Read answers aloud"),
    full_text: bool = typer.Option(
        False, "--full-text", help="Speak the written answer verbatim, citations and all"
    ),
    mcp: bool = typer.Option(True, help="Mount MCP servers from yoyo-mcp.yaml"),
) -> None:
    """Push-to-talk conversation. ENTER starts recording, ENTER stops it.

    Not held-key push-to-talk: reading a physical key state needs a raw console handler that
    behaves differently across Windows terminals, and a voice loop that works in one shell
    and silently fails in another is worse than one extra keypress.

    Two things differ from the written path, both deliberate. Each turn is **routed** (say
    "plan:" or "use ask mode" out loud to override), and what is *spoken* is a reshaped
    answer — citations counted rather than recited, code announced rather than spelled out.
    The written answer stays on screen unchanged and remains the source of truth.
    """
    _setup_logging()
    if mode not in {"auto", "ask", "agent", "plan"}:
        raise typer.BadParameter("mode must be auto, ask, agent or plan")

    cfg = voice_mod.load_config()
    stt = voice_mod.get_transcriber(cfg)
    if not stt.is_available():
        console.print('[red]faster-whisper is not installed.[/] uv pip install -e ".[voice]"')
        raise typer.Exit(1)

    speaker = None
    if speak_reply:
        speaker = voice_mod.get_speaker(cfg)
        if not speaker.is_available():
            console.print(f"[yellow]{speaker.name} unusable — printing answers only.[/]")
            speaker = None

    if mcp and mode != "ask":
        for name, r in mcp_client.mount_all().items():
            if not r["ok"]:
                console.print(f"[yellow]mcp {name}: {escape(r['error'])}[/]")

    from .voice.mic import Recorder

    console.print(
        f"[bold]Talk[/] — mode [cyan]{mode}[/]. "
        f"ENTER to start recording, ENTER again to stop. Ctrl-C or an empty line to quit.\n"
        f"[dim]Audio is transcribed on this laptop; only the text reaches the model.[/]"
    )
    # Load the model before the first turn, otherwise the user speaks into a void while a
    # multi-hundred-MB model loads and assumes the microphone is broken.
    console.print("[dim]loading the speech model...[/]")
    stt.load()

    conversation: int | None = None
    while True:
        try:
            if input("\n[press ENTER to speak] ").strip():
                break
        except (EOFError, KeyboardInterrupt):
            break

        try:
            with Recorder(device=device if device is not None else cfg.mic_device) as rec:
                console.print("[red]● recording[/] — press ENTER to stop")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
                rec.drain()
                seconds = rec.seconds
        except voice_mod.AudioDeviceError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            raise typer.Exit(1) from exc

        if rec.too_short:
            console.print(f"[yellow]only {seconds:.1f}s captured — ignoring.[/]")
            continue

        heard = stt.transcribe_pcm(rec.pcm, 16_000, language=cfg.stt_language)
        question = heard.text.strip()
        console.print(f"[dim]heard ({seconds:.1f}s → {heard.latency_ms / 1000:.1f}s):[/] "
                      f"[bold]{escape(question)}[/]")
        if not question:
            console.print("[yellow]Nothing recognised. Try again closer to the mic.[/]")
            continue

        try:
            answer = router.run(
                question,
                override=None if mode == "auto" else mode,
                conversation_id=conversation,
            )
        except router.RouteError as exc:
            console.print(f"[yellow]{escape(str(exc))}[/]")
            continue

        conversation = answer.conversation_id or conversation
        text = answer.text
        console.print(f"[cyan]▸ {answer.route.mode}[/] [dim]— {escape(answer.route.reason)}[/]")
        if answer.fabricated_links:
            console.print(
                f"[yellow]removed {len(answer.fabricated_links)} fabricated path(s)[/]"
            )

        console.print(escape(text))
        console.print(f"[dim]{answer.model} · {answer.latency_ms} ms · {answer.detail}[/]")
        if speaker:
            # Announce the route out loud before answering. With no screen to glance at,
            # an unannounced routing decision is invisible — and an invisible decision is
            # one the owner cannot override, which is the whole design constraint.
            spoken = text if full_text else voice_speech.for_speech(text)
            if mode == "auto" and not full_text:
                spoken = f"{voice_speech.route_announcement(answer.route.mode)} {spoken}"
            try:
                speaker.speak(spoken)
            except voice_mod.VoiceError as exc:
                console.print(f"[yellow]could not speak: {escape(str(exc))}[/]")

    console.print("[dim]bye[/]")

calendar_app = typer.Typer(help="Calendar: Google and Microsoft 365. Read only.")
app.add_typer(calendar_app, name="calendar")


@calendar_app.command("accounts")
def calendar_accounts() -> None:
    """Configured calendar accounts and whether each is authenticated."""
    _setup_logging()
    rows = cal_mod.status()
    if not rows:
        console.print("[yellow]No accounts in yoyo-calendar.yaml[/]")
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold")
    table.add_column("account")
    table.add_column("provider")
    table.add_column("enabled")
    table.add_column("authenticated")
    table.add_column("note", overflow="fold")
    for r in rows:
        auth = r.get("authenticated")
        table.add_row(
            r["account"],
            r["provider"],
            "yes" if r["enabled"] else "[dim]no[/]",
            "[green]yes[/]" if auth else ("[red]no[/]" if r["enabled"] else "[dim]-[/]"),
            escape(r.get("error") or r.get("description") or ""),
        )
    console.print(table)
    console.print(f"[dim]tokens: {cal_mod.token_dir()}[/]")
    console.print("[dim]Read-only: Yoyo cannot create, change, delete or RSVP to events.[/]")


@calendar_app.command("auth")
def calendar_auth(account: str) -> None:
    """Run the OAuth consent flow for one calendar account."""
    _setup_logging()
    specs = {s.name: s for s in cal_mod.load_accounts()}
    if account not in specs:
        raise typer.BadParameter(
            f"unknown account {account!r}. Configured: {', '.join(sorted(specs)) or '<none>'}"
        )
    console.print(
        "[yellow]Note:[/] this stores a refresh token for your calendar under "
        f"{cal_mod.token_dir()}. The disk is not encrypted (OQ4).\n"
        "[dim]Scope requested is read-only — calendar.readonly / Calendars.Read.[/]"
    )
    provider = cal_mod.build(specs[account])
    console.print(f"[green]{provider.authenticate()}[/]")
    if not specs[account].enabled:
        console.print(
            f"[yellow]{account} is still `enabled: false` in yoyo-calendar.yaml — "
            f"set it to true or nothing will use this token.[/]"
        )


@calendar_app.command("agenda")
def calendar_agenda(
    day: Optional[str] = typer.Option(None, help="YYYY-MM-DD, default today"),  # noqa: UP045
    days: int = typer.Option(1, help="How many days forward"),
    account: Optional[str] = typer.Option(None),  # noqa: UP045
) -> None:
    """Show the agenda, merged across every enabled account."""
    _setup_logging()
    from datetime import date as _date
    from datetime import datetime as _datetime

    target = _date.today()
    if day:
        try:
            target = _datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise typer.BadParameter("day must be YYYY-MM-DD") from exc

    events = cal_mod.agenda(day=target, days=days, account=account)
    if not events:
        console.print(f"[dim]Nothing scheduled for {target} (+{days - 1} days).[/]")
        raise typer.Exit(0)

    for e in events:
        when = "all day" if e.all_day else (e.start.strftime("%a %d %b %H:%M") if e.start else "?")
        flag = "[red]✗[/]" if e.response == "declined" else " "
        console.print(f"{flag} [bold]{escape(when)}[/] {escape(e.title)}")
        bits = []
        if e.duration_minutes:
            bits.append(f"{e.duration_minutes} min")
        if e.location:
            bits.append(escape(e.location))
        if e.online_url:
            bits.append("online")
        if e.attendees:
            bits.append(f"{len(e.attendees)} attendees")
        if bits:
            console.print(f"   [dim]{' · '.join(bits)}[/]")

    clashes = cal_mod.find_conflicts(events)
    summary = cal_mod.summarise(events)
    console.print(
        f"\n[dim]{summary['count']} events · {summary['busy_minutes']} busy minutes · "
        f"{summary['declined']} declined[/]"
    )
    for a, b in clashes:
        console.print(f"[yellow]clash:[/] {escape(a.title)} ↔ {escape(b.title)}")


@calendar_app.command("search")
def calendar_search(
    query: str,
    account: Optional[str] = typer.Option(None),  # noqa: UP045
    limit: int = typer.Option(10),
) -> None:
    """Search calendar events without involving a model."""
    _setup_logging()
    provider = cal_mod.resolve(account)
    for e in provider.search(query, limit=limit):
        when = e.start.strftime("%Y-%m-%d %H:%M") if e.start else "?"
        console.print(f"[dim]{when}[/] [bold]{escape(e.title)}[/]")
        if e.location:
            console.print(f"   [dim]{escape(e.location)}[/]")


tasks_app = typer.Typer(help="Tasks from the vault's Markdown checkboxes. Read only.")
app.add_typer(tasks_app, name="tasks")


@tasks_app.command("list")
def tasks_list(
    status: str = typer.Option("open", help="open | done | all"),
    due_before: Optional[str] = typer.Option(None, help="YYYY-MM-DD"),  # noqa: UP045
    tag: Optional[str] = typer.Option(None),  # noqa: UP045
    contains: Optional[str] = typer.Option(None),  # noqa: UP045
    folder: str = typer.Option("", help="Restrict to a vault subfolder"),
    limit: int = typer.Option(50),
) -> None:
    """List tasks found in the vault, soonest deadline first."""
    _setup_logging()
    try:
        items = tasks_mod.query(
            status=status, folder=folder, due_before=due_before,
            tag=tag, contains=contains, limit=limit,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None

    if not items:
        console.print("[dim]No matching tasks.[/]")
        raise typer.Exit(0)

    from datetime import date as _date

    today = _date.today()
    for t in items:
        box = "[green]x[/]" if not t.open else " "
        due = ""
        if t.due:
            colour = "red" if t.overdue(today) else "cyan"
            due = f" [{colour}]{t.due}[/]"
        pri = f" [magenta]{t.priority}[/]" if t.priority else ""
        tags = f" [dim]{' '.join('#' + x for x in t.tags)}[/]" if t.tags else ""
        console.print(f"[{box}] {escape(t.text)}{due}{pri}{tags}")
        console.print(f"    [dim]{escape(t.note)}:{t.line}[/]")


@tasks_app.command("summary")
def tasks_summary() -> None:
    """Counts only — total, open, overdue, undated."""
    _setup_logging()
    for k, v in tasks_mod.summary().items():
        console.print(f"{k:20} {v}")

@mail_app.command("read")
def mail_read(
    message_id: str,
    account: Optional[str] = typer.Option(None),  # noqa: UP045
    body: bool = typer.Option(True, "--body/--no-body"),
) -> None:
    """Read one message by id. This is how you resolve a [mail:<id>] citation.

    A citation nobody can follow is decoration. Corpus answers cite chunk ids you can look
    up with `yoyo search`; this is the mail equivalent, and it exists so an answer about
    your inbox is checkable without hunting through Gmail by hand.
    """
    _setup_logging()
    provider = mail_mod.resolve(account)
    m = provider.read(message_id.removeprefix("mail:"))
    when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
    console.print(f"[bold]{escape(m.subject)}[/]")
    console.print(f"[dim]from[/] {escape(m.sender)}")
    console.print(f"[dim]to[/]   {escape(', '.join(m.to))}")
    console.print(f"[dim]{when} · {escape(m.citation)}[/]\n")
    if body and m.body:
        console.print(escape(m.body[:8000]))


@mail_app.command("draft")
def mail_draft(
    to: str = typer.Option(..., help="Comma-separated recipients"),
    subject: str = typer.Option(...),
    body: str = typer.Option(..., help="Plain text body"),
    cc: Optional[str] = typer.Option(None),  # noqa: UP045
    reply_to: Optional[str] = typer.Option(None, help="Message id to thread the reply under"),  # noqa: UP045
    account: Optional[str] = typer.Option(None),  # noqa: UP045
) -> None:
    """Save a draft. It is NOT sent — Yoyo has no send path, by design.

    Exposed on the CLI so the draft path can be exercised deliberately, on a message you
    chose, rather than first discovered when an agent writes one unprompted.
    """
    _setup_logging()
    provider = mail_mod.resolve(account)
    draft = provider.create_draft(
        to=[a.strip() for a in to.split(",") if a.strip()],
        subject=subject,
        body=body,
        cc=[a.strip() for a in cc.split(",")] if cc else None,
        reply_to_message_id=(reply_to.removeprefix("mail:") if reply_to else None),
    )
    console.print(f"[green]saved draft[/] {escape(draft.id)} — [bold]not sent[/]")
    console.print("[dim]Open Gmail → Drafts to review and send it yourself.[/]")


web_app = typer.Typer(help="Web search and fetching, via your self-hosted SearXNG.")
app.add_typer(web_app, name="web")


@web_app.command("search")
def web_search(query: str, limit: int = typer.Option(8)) -> None:
    """Search the web without involving a model. The query leaves this machine."""
    _setup_logging()
    try:
        results = web_mod.search(query, limit=limit)
    except web_mod.SearchError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None
    if not results:
        console.print("[yellow]No results.[/]")
        raise typer.Exit(0)
    for r in results:
        console.print(f"[bold]{escape(r.title)}[/]")
        console.print(f"  [cyan]{escape(r.url)}[/]")
        if r.snippet:
            console.print(f"  [dim]{escape(r.snippet[:200])}[/]")
        console.print()
    console.print("[dim]Logged to data/egress.jsonl — `yoyo web egress`[/]")


@web_app.command("fetch")
def web_fetch(url: str, chars: int = typer.Option(4000, help="How much text to print")) -> None:
    """Fetch one page and print its readable text."""
    _setup_logging()
    try:
        page = web_mod.fetch(url)
    except web_mod.BlockedTarget as exc:
        console.print(f"[red]refused:[/] {escape(str(exc))}")
        raise typer.Exit(2) from None
    except web_mod.SearchError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from None
    console.print(f"[bold]{escape(page.title)}[/]")
    console.print(f"[dim]{escape(page.url)} · {page.fetched_ms} ms"
                  f"{' · truncated' if page.truncated else ''}[/]\n")
    console.print(escape(page.text[:chars]))


@web_app.command("egress")
def web_egress(limit: int = typer.Option(30)) -> None:
    """What Yoyo has sent to the internet. The audit ADR-009 promised and Windows lost."""
    _setup_logging()
    entries = web_mod.read_egress(limit=limit)
    if not entries:
        console.print("[dim]Nothing sent yet.[/]")
        raise typer.Exit(0)
    table = Table(show_header=True, header_style="bold")
    table.add_column("when")
    table.add_column("kind")
    table.add_column("target / query", overflow="fold")
    for e in entries:
        table.add_row(e.get("at", "?"), e.get("kind", "?"),
                      escape(e.get("detail") or e.get("target", "")))
    console.print(table)
    console.print(f"[dim]{web_mod.egress_log_path()}[/]")


def _require_encrypted_disk(what: str, force: bool) -> None:
    """The owner's own condition, enforced by the machine instead of by memory.

    The agreed gate was "BitLocker before real data goes in". A gate that lives in a README
    row and in someone's head does not hold at 11pm three weeks later, when the command is
    routine and the reason it existed has faded. So the two commands that put personal
    material on this disk check, and stop.

    It **stops**, it does not silently proceed with a warning — a warning printed above a
    green summary line is a warning nobody reads twice. `--force` is there because testing
    on throwaway content is legitimate and was always allowed; typing it is the point.

    UNKNOWN status proceeds. Refusing to work because a check could not run would be
    punishing the user for an elevated-shell requirement, and this is a gate, not a lock.
    """
    encrypted = doctor.disk_is_encrypted()
    if encrypted is None:
        # Observed 2026-08-15: on a normal PowerShell the BitLocker probe returns "Access
        # denied", so this branch — not the refusal — is what the owner's machine actually
        # hits. A gate that silently waves through the case it always lands in is not a
        # gate. It still proceeds (unknown is not evidence of absence) but it says so, so
        # "I never saw a warning" cannot be mistaken for "it checked and was happy".
        console.print(
            "[yellow]Could not confirm this disk is encrypted[/] "
            "[dim](BitLocker status needs an elevated shell — run `yoyo doctor` as "
            "administrator to check). Proceeding.[/]"
        )
        return
    if encrypted:
        return
    console.print(f"[red]Refusing to {what} — this disk is not encrypted.[/]\n")
    console.print(doctor.ENCRYPTION_WARNING)
    console.print(
        "\n[dim]This is the condition you set yourself: BitLocker before real data goes in.\n"
        "Testing on throwaway content is fine — re-run with --force.[/]"
    )
    if not force:
        raise typer.Exit(3)
    console.print("\n[yellow]--force given; continuing on an unencrypted disk.[/]\n")


@app.command()
def remember(
    conversation: Optional[int] = typer.Option(None, help="One conversation id; default all"),  # noqa: UP045
    min_turns: int = typer.Option(1, help="Skip conversations shorter than this"),
    force: bool = typer.Option(False, "--force", help="Proceed even on an unencrypted disk"),
) -> None:
    """Make past conversations searchable — Phase 1 of memory.

    Verbatim only. This stores what was said, it does not summarise or extract anything, and
    that is the point: everything the wiki layer writes later must trace back to a raw source,
    and this is what a raw source is.
    """
    _setup_logging()
    _require_encrypted_disk("store transcripts of your conversations", force)
    from .memory import sources as memory_sources

    report = memory_sources.remember(
        conversation_ids=[conversation] if conversation else None,
        min_turns=min_turns,
    )
    console.print(f"[green]{report.summary()}[/]")
    if not report.conversations:
        console.print("[dim]Nothing to remember yet — have a conversation first.[/]")
        return
    console.print("[dim]Past conversations are now searchable: `yoyo search \"...\"`[/]")


memory_app = typer.Typer(help="Yoyo's memory — the second brain.")
app.add_typer(memory_app, name="memory")


@memory_app.command("build")
def memory_build(
    conversation: Optional[int] = typer.Option(None, help="One conversation; default all"),  # noqa: UP045
    notes: bool = typer.Option(True, help="Also read your own vault notes"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be written. Writes nothing."
    ),
    force: bool = typer.Option(False, "--force", help="Proceed even on an unencrypted disk"),
) -> None:
    """Write memory pages from raw sources.

    Every claim must quote its source verbatim, and no claim may cite another memory page.
    Those two gates are why this can run without asking your permission first.
    """
    _setup_logging()
    # A dry run writes nothing, so the encryption gate does not apply to it. Blocking the
    # one command that lets you INSPECT memory before trusting it would push you toward
    # running the real thing to find out what it does.
    if not dry_run:
        _require_encrypted_disk("write pages about the people in your life", force)
    from . import vault as vault_mod
    from .memory import build as build_mod
    from .memory import sources as source_mod

    raw: dict[str, str] = {}
    for row in core.list_conversations(limit=1000):
        cid = int(row["id"])
        if conversation and cid != conversation:
            continue
        text, turns, _ = source_mod.render(cid, row.get("title"),
                                           core.conversation_messages(cid))
        if turns:
            raw[source_mod.source_path(cid)] = text

    if notes:
        root = vault_mod.vault_root()
        for note in vault_mod._notes(root):
            raw[f"vault://{vault_mod._rel(note, root)}"] = note.read_text(
                encoding="utf-8", errors="replace")

    if not raw:
        console.print("[yellow]No raw sources yet. Have a conversation or write a note.[/]")
        raise typer.Exit(0)

    console.print(f"[dim]reading {len(raw)} raw source(s)…[/]")
    report = build_mod.build(raw, dry_run=dry_run)
    console.print(f"[{'yellow' if dry_run else 'green'}]{report.summary()}[/]")

    if dry_run:
        # Print the claims themselves, not just counts. The question a dry run exists to
        # answer — "is this worth keeping?" — cannot be answered by a number, and a claim
        # is only judgeable next to the quote it rests on.
        for page in report.pages:
            console.print(f"\n[bold]{escape(page.subject)}[/] [dim]({page.kind}) → "
                          f"{escape(page.path)}[/]")
            for claim in page.claims:
                console.print(f"  • {escape(claim.claim)}")
                console.print(f"    [dim]{escape(claim.source)} — "
                              f"\"{escape(claim.quote.strip()[:160])}\"[/]")
        if not report.pages:
            console.print("[dim]Nothing proposed. For a real conversation that is a "
                          "finding, not a bug — read it as 'no durable facts here'.[/]")

    for subject, why in report.rejected[:10]:
        console.print(f"[yellow]rejected[/] {escape(subject)}: {escape(why)}")
    for question in report.ambiguities:
        # Asked, never guessed: merging two people makes a page wrong about both.
        console.print(f"[cyan]?[/] {escape(question['question'])}")
    if report.ambiguities:
        console.print("[dim]Answer by writing the note yourself, or tell me in a chat.[/]")


@memory_app.command("show")
def memory_show(subject: Optional[str] = typer.Argument(None)) -> None:  # noqa: UP045
    """List memory pages, or show one."""
    _setup_logging()
    from . import vault as vault_mod
    from .memory import build as build_mod

    root = vault_mod.vault_root()
    pages = build_mod.load_pages(root)
    if not pages:
        console.print("[dim]No memory yet — run `yoyo memory build`.[/]")
        raise typer.Exit(0)

    if subject is None:
        table = Table(show_header=True, header_style="bold")
        table.add_column("kind")
        table.add_column("subject")
        table.add_column("claims", justify="right")
        for (kind, name), claims in sorted(pages.items()):
            table.add_row(kind, escape(name), str(len(claims)))
        console.print(table)
        return

    for (kind, name), claims in pages.items():
        if name != subject.lower():
            continue
        console.print(f"[bold]{escape(subject)}[/] [dim]({kind})[/]\n")
        for claim in claims:
            console.print(f"  • {escape(claim.claim)}")
            console.print(f"    [dim]{escape(claim.source)} — \"{escape(claim.quote)}\"[/]")
        return
    console.print(f"[yellow]No memory page for {escape(subject)}.[/]")


@memory_app.command("sweep")
def memory_sweep(
    idle: Optional[int] = typer.Option(None, help="Override idle_minutes for this run"),  # noqa: UP045
) -> None:
    """Read every idle conversation Yoyo has not read yet, and queue what it finds.

    This is what the scheduler runs automatically every few minutes while `yoyo serve` is
    up. Running it by hand is for catching up after the laptop has been shut, or for
    watching what a sweep actually does.
    """
    _setup_logging()
    from .memory import pipeline

    cfg = pipeline.load_config()
    if idle is not None:
        cfg.idle_minutes = idle
    report = pipeline.sweep(cfg, on_log=lambda line: console.print(f"[dim]{escape(line)}[/]"))
    console.print(f"[green]{report.summary()}[/]")
    if report.capped:
        console.print("[yellow]The review queue is full. Clear some of it and run again — "
                      "the sweep resumes exactly where it stopped.[/]")
    if report.queued:
        console.print("[dim]Review them: `yoyo memory review`, or the Memory tab.[/]")


@memory_app.command("status")
def memory_status() -> None:
    """What continuous memory has done, and what it is waiting on."""
    _setup_logging()
    from .memory import pipeline

    st = pipeline.status()
    table = Table(show_header=False, box=None)
    table.add_row("sweeping", "enabled" if st["config"]["enabled"] else "[yellow]disabled[/]")
    table.add_row("idle trigger", f"{st['config']['idle_minutes']} min")
    table.add_row("nightly", f"{st['config']['nightly_hour']:02d}:00")
    table.add_row("conversations read", str(st["conversations_swept"]))
    table.add_row("claims ever queued", str(st["claims_ever_queued"]))
    table.add_row("last sweep", str(st["last_sweep"] or "never"))
    table.add_row("waiting to be read", str(st["waiting"]))
    table.add_row("ignored conversations", str(st["conversations_ignored"]))
    table.add_row("queue", str(st["queue"]))
    console.print(table)
    if st["capped"]:
        console.print(f"[yellow]Queue is at the cap ({st['config']['queue_cap']}). Sweeping "
                      f"is paused until you clear some.[/]")


@memory_app.command("ignore")
def memory_ignore(
    conversation: int = typer.Argument(..., help="Conversation id"),
    undo: bool = typer.Option(False, "--undo", help="Start remembering it again"),
) -> None:
    """Tell the sweep to leave a conversation alone.

    Not the same as forgetting. This stops future reading; what was already extracted stays
    where it is, and unsaying that is `yoyo memory forget`, which leaves a tombstone. Two
    different acts, and conflating them would make one of them silent.
    """
    _setup_logging()
    from .memory import pipeline

    if not pipeline.set_remember(conversation, undo):
        console.print(f"[yellow]no conversation {conversation}[/]")
        raise typer.Exit(1)
    console.print(f"[green]conversation {conversation}: "
                  f"{'remembered again' if undo else 'will be ignored by the sweep'}[/]")


@memory_app.command("review")
def memory_review(
    limit: int = typer.Option(50, help="How many pending claims to show"),
) -> None:
    """Claims waiting for your decision, with the quote each one rests on.

    The gates prove a claim is traceable. Only you can say whether it is worth keeping —
    six pages of world history passed every gate on the first real run.
    """
    _setup_logging()
    from .memory import review as review_mod

    rows = review_mod.pending(limit=limit)
    stats = review_mod.stats()
    if not rows:
        console.print("[dim]Nothing pending. Run `yoyo memory propose` first.[/]")
    subject = None
    for row in rows:
        if row["subject"] != subject:
            subject = row["subject"]
            console.print(f"\n[bold]{escape(subject)}[/] [dim]({row['kind']})[/]")
        console.print(f"  [cyan]#{row['id']}[/] {escape(row['claim'])}")
        console.print(f"      [dim]{escape(row['source'])} — "
                      f"\"{escape((row['quote'] or '')[:140])}\"[/]")
    console.print(f"\n[dim]{stats}[/]")
    console.print("[dim]Decide in the UI (Memory tab), or `yoyo memory decide <id> "
                  "--reject`.[/]")


@memory_app.command("propose")
def memory_propose(notes: bool = typer.Option(True, help="Also read your own vault notes")) -> None:
    """Extract and queue claims for review. Writes nothing to the vault."""
    _setup_logging()
    from . import vault as vault_mod
    from .memory import build as build_mod
    from .memory import sources as source_mod

    raw: dict[str, str] = {}
    for row in core.list_conversations(limit=1000):
        cid = int(row["id"])
        text, turns, _ = source_mod.render(cid, row.get("title"),
                                           core.conversation_messages(cid))
        if turns:
            raw[source_mod.source_path(cid)] = text
    if notes:
        root = vault_mod.vault_root()
        for note in vault_mod._notes(root):
            raw[f"vault://{vault_mod._rel(note, root)}"] = note.read_text(
                encoding="utf-8", errors="replace")

    console.print(f"[dim]reading {len(raw)} raw source(s)…[/]")
    report = build_mod.propose_for_review(raw)
    console.print(f"[green]queued {report['queued']} new claim(s)[/] "
                  f"[dim](accepted={report['accepted']} "
                  f"already_pending={report['already_pending']} "
                  f"previously_decided={report['previously_decided']})[/]")
    console.print("[dim]Review them: `yoyo memory review`[/]")


@memory_app.command("decide")
def memory_decide(
    proposal_id: int = typer.Argument(..., help="Proposal id from `yoyo memory review`"),
    reject: bool = typer.Option(False, "--reject", help="Reject instead of approving"),
) -> None:
    """Approve or reject one proposed claim. Rejection is permanent — it is never re-asked."""
    _setup_logging()
    from .memory import review as review_mod

    status = "rejected" if reject else "approved"
    if review_mod.decide(proposal_id, status):
        console.print(f"[green]#{proposal_id} {status}[/]")
    else:
        console.print(f"[yellow]#{proposal_id} was already decided, or does not exist[/]")


@memory_app.command("apply")
def memory_apply(
    force: bool = typer.Option(False, "--force", help="Proceed even on an unencrypted disk"),
) -> None:
    """Write the claims you approved. The only path from a proposal to a page."""
    _setup_logging()
    _require_encrypted_disk("write pages about the people in your life", force)
    from .memory import build as build_mod

    result = build_mod.apply_approved()
    if not result["claims"]:
        console.print("[yellow]Nothing approved yet — `yoyo memory review` first.[/]")
        return
    console.print(f"[green]wrote {result['claims']} claim(s) across "
                  f"{result['pages']} page(s)[/]")
    if result.get("flagged"):
        console.print(f"[yellow]{result['flagged']} possible contradiction(s) flagged — "
                      f"both claims kept, neither resolved[/]")


@memory_app.command("forget")
def memory_forget(
    subject: str,
    containing: Optional[str] = typer.Option(None, help="Only claims mentioning this"),  # noqa: UP045
) -> None:
    """Forget a subject, or specific claims about them.

    This really deletes. The log records that something was forgotten and when — never what,
    because a tombstone repeating the memory would not be forgetting.
    """
    _setup_logging()
    from . import vault as vault_mod
    from .memory import build as build_mod

    touched = build_mod.forget(vault_mod.vault_root(), subject, contains=containing)
    if touched:
        console.print(f"[green]forgot {escape(subject)}[/] ({touched} page(s))")
    else:
        console.print(f"[yellow]nothing to forget about {escape(subject)}[/]")


if __name__ == "__main__":
    app()
