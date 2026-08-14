"""Yoyo CLI.  `yoyo --help`"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import backup as backup_mod
from . import bench as bench_mod
from . import agent as agent_mod
from . import core, doctor
from .graph import GraphBudget
from .graph import run as graph_run
from . import evals as evals_mod
from . import mail as mail_mod
from . import tools as tools_mod
from .mcp import client as mcp_client
from .config import get_settings
from .rag import ingest as ingest_mod
from .rag import retrieve as rag
from .storage import db, vectors

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
        console.print(f"   [dim]{escape(m.snippet[:140])}[/]")
        console.print(f"   [dim]id={m.id}[/]\n")


if __name__ == "__main__":
    app()
