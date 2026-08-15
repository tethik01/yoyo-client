"""Preflight checks. Run this before believing anything else works.

Checks the seams that actually break: the tailnet hop, the auth key, role/endpoint drift,
the tool-fidelity constraint, embeddings, schema version, Qdrant.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import embeddings, llm
from .config import NO_TOOLS_ENDPOINTS, env_summary, get_models, get_settings
from .storage import db, vectors


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_all() -> list[Check]:
    checks: list[Check] = [_env()]
    served = _server(checks)
    _roles(checks, served)
    _tool_fidelity(checks)
    _embeddings(checks)
    _sqlite(checks)
    _qdrant(checks)
    _vault(checks)
    _optional_configs(checks)
    return checks


def _env() -> Check:
    s = get_settings()
    problems = []
    if not s.llm_api_key:
        problems.append("YOYO_LLM_API_KEY unset")
    if "REPLACE-ME" in s.llm_base_url:
        problems.append("YOYO_LLM_BASE_URL is still the placeholder")
    if s.request_timeout < 900:
        problems.append(
            f"YOYO_REQUEST_TIMEOUT={s.request_timeout}s is below the server's 900s; "
            f"agent tool loops will look like failures"
        )
    duplicates = duplicate_env_keys()
    if duplicates:
        # Observed live: `.env` carried two YOYO_VAULT_PATH lines after an edit. dotenv
        # resolves to the last one, so it worked — but which value was in force was
        # invisible to anyone reading the file, and the losing line looked authoritative.
        # A config that behaves differently from how it reads is the same class of problem
        # as a doc that disagrees with the code.
        problems.append(
            "duplicate keys in .env: " + ", ".join(duplicates)
            + " (the LAST occurrence wins — delete the others)"
        )

    if problems:
        return Check("env", False, "; ".join(problems))
    return Check("env", True, f"{s.llm_base_url} timeout={s.request_timeout}s")


def duplicate_env_keys(path=None) -> list[str]:  # noqa: ANN001
    """Keys assigned more than once in `.env`, in file order."""
    from .config import REPO_ROOT

    path = path or (REPO_ROOT / ".env")
    if not path.exists():
        return []
    seen: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        seen[key] = seen.get(key, 0) + 1
    return [k for k, n in seen.items() if n > 1]


def _server(checks: list[Check]) -> list[str]:
    try:
        served = llm.list_models()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check("server reachable", False, f"{exc} — is Tailscale up and MyAIServer online?")
        )
        return []
    checks.append(Check("server reachable", True, f"serves: {', '.join(served)}"))
    return served


def _roles(checks: list[Check], served: list[str]) -> None:
    if not served:
        checks.append(Check("roles", False, "skipped — server unreachable"))
        return
    cfg = get_models()
    missing = [f"{n} -> {r.endpoint}" for n, r in cfg.roles.items() if r.endpoint not in served]
    if missing:
        checks.append(
            Check("roles", False, "endpoints not served: " + "; ".join(missing))
        )
    else:
        mapping = ", ".join(f"{n}->{r.endpoint}" for n, r in cfg.roles.items())
        checks.append(Check("roles", True, mapping))


def _tool_fidelity(checks: list[Check]) -> None:
    """A tools:true role pointed at a non-tool-reliable endpoint is a correctness bug."""
    bad = [
        f"{n} -> {r.endpoint}"
        for n, r in get_models().roles.items()
        if r.tools and r.endpoint in NO_TOOLS_ENDPOINTS
    ]
    if bad:
        checks.append(
            Check(
                "tool fidelity",
                False,
                "tool-using roles on a fabricating endpoint: "
                + "; ".join(bad)
                + ". Point them at a capability that has PASSED the fidelity gate "
                "(`coder`, or `agent` as the fallback). This said \"point them at "
                "'agent'\" until 2026-08-15 — the rule was never about `agent`, it was "
                "about having evidence, and `agent` was the only one that had any.",
            )
        )
        return
    tool_roles = [n for n, r in get_models().roles.items() if r.tools]
    checks.append(
        Check(
            "tool fidelity",
            True,
            f"tool roles ({', '.join(tool_roles) or 'none'}) all avoid "
            f"{'/'.join(sorted(NO_TOOLS_ENDPOINTS))}",
        )
    )


def _embeddings(checks: list[Check]) -> None:
    cfg = get_models().embeddings
    where = cfg.local_model if cfg.provider == "local" else cfg.endpoint
    try:
        vec = embeddings.embed(["yoyo preflight"])[0]
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("embeddings", False, f"[{cfg.provider}:{where}] {exc}"))
        return
    checks.append(
        Check("embeddings", True, f"{cfg.provider}:{where} dim={len(vec)}")
    )


def _sqlite(checks: list[Check]) -> None:
    try:
        with db.connection() as conn:
            version = db.current_version(conn)
            if version == 0:
                checks.append(Check("sqlite", False, "no schema — run `yoyo migrate`"))
                return
            st = db.stats(conn)
        checks.append(
            Check(
                "sqlite",
                True,
                f"v{version}, docs={st['documents']} chunks={st['chunks']} "
                f"embedded={st['chunks_embedded']}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("sqlite", False, str(exc)))


def _qdrant(checks: list[Check]) -> None:
    try:
        i = vectors.info()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check("qdrant", False, f"{exc} — is Docker Desktop running? `docker compose up -d`")
        )
        return
    if not i["exists"]:
        checks.append(Check("qdrant", True, "reachable; collection not created yet (ingest will)"))
        return
    ok = i["dimensions"] == embeddings.dimensions()
    checks.append(
        Check(
            "qdrant",
            ok,
            f"points={i['points']} dim={i['dimensions']} status={i['status']}"
            + ("" if ok else f" — MISMATCH vs embed dim {embeddings.dimensions()}; "
                           "reindex --recreate"),
        )
    )


def _vault(checks: list[Check]) -> None:
    """The vault is canon and is easy to get subtly wrong.

    Two failures already cost real time: an empty YOYO_VAULT_PATH resolved to `.` and made
    the working directory the vault, and pointing at `test-vault` while believing it was the
    real one produces answers that are correct about the wrong corpus. Neither raises.
    """
    from . import vault

    try:
        root = vault.vault_root()
    except vault.VaultError as exc:
        message = exc.args[0]
        # UNSET is a legitimate state — vault features are optional, and failing doctor over
        # one would train the user to ignore a red check.
        # MISCONFIGURED is not: a path that points at a file, or at somewhere that no longer
        # exists, means vault tools will fail at the worst moment with no warning here.
        if "No vault configured" in message:
            checks.append(Check("vault", True, "not configured (vault features are off)"))
        else:
            checks.append(Check("vault", False, f"YOYO_VAULT_PATH is set but unusable: {message}"))
        return

    notes = vault._notes(root)
    detail = f"{root} — {len(notes)} notes"
    if root.name == "test-vault" or len(notes) < 5:
        # A warning, not a failure: a small vault is valid, it is just very often the
        # scaffold rather than the real thing.
        detail += "  [looks like the test scaffold, not a real Obsidian vault]"
    checks.append(Check("vault", True, detail))


def _optional_configs(checks: list[Check]) -> None:
    """Config files parse, and optional extras are present or honestly absent.

    Deliberately does NOT touch the network or any credential — `yoyo doctor` must stay
    fast and runnable offline. Authentication status is `yoyo mail accounts` /
    `yoyo calendar accounts`, which is a different question.
    """
    notes: list[str] = []
    problems: list[str] = []

    for label, loader in (("mail", _mail_accounts), ("calendar", _calendar_accounts)):
        try:
            total, enabled = loader()
        except Exception as exc:  # noqa: BLE001 - a malformed yaml must not kill doctor
            problems.append(f"{label} config: {exc}")
            continue
        notes.append(f"{label} {enabled}/{total} enabled" if total else f"{label} none configured")

    try:
        from . import voice

        cfg = voice.load_config()
        stt = voice.get_transcriber(cfg)
        speaker = voice.get_speaker(cfg)
        notes.append(
            f"voice stt={cfg.stt_model}"
            f"{'' if stt.is_available() else ' (engine NOT installed)'}"
            f" tts={speaker.name}{'' if speaker.is_available() else ' (unusable)'}"
        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"voice config: {exc}")

    if problems:
        checks.append(Check("optional configs", False, "; ".join(problems)))
        return
    checks.append(Check("optional configs", True, " · ".join(notes)))


def _mail_accounts() -> tuple[int, int]:
    from . import mail

    specs = mail.load_accounts()
    return len(specs), sum(1 for s in specs if s.enabled)


def _calendar_accounts() -> tuple[int, int]:
    from . import calendar as cal

    specs = cal.load_accounts()
    return len(specs), sum(1 for s in specs if s.enabled)


def summary() -> dict[str, object]:
    checks = run_all()
    return {
        "ok": all(c.ok for c in checks),
        "env": env_summary(),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }
