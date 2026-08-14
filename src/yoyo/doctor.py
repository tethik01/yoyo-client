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
    if problems:
        return Check("env", False, "; ".join(problems))
    return Check("env", True, f"{s.llm_base_url} timeout={s.request_timeout}s")


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
                + ". Point them at 'agent'.",
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
            + ("" if ok else f" — MISMATCH vs embed dim {embeddings.dimensions()}; reindex --recreate"),
        )
    )


def summary() -> dict[str, object]:
    checks = run_all()
    return {
        "ok": all(c.ok for c in checks),
        "env": env_summary(),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }
