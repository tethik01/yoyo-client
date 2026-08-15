"""Configuration. Everything comes from .env or yoyo-models.yaml — nothing is hardcoded."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_FILE = REPO_ROOT / "yoyo-models.yaml"

#: Capability names on MyAIServer that are NOT tool-reliable. Passing tools to one of
#: these is a correctness bug (see yoyo-models.yaml header). Enforced in llm.py.
NO_TOOLS_ENDPOINTS = frozenset({"fast"})


class Settings(BaseSettings):
    """Environment-backed settings. Prefix YOYO_."""

    model_config = SettingsConfigDict(
        env_prefix="YOYO_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "http://localhost:4000/v1"
    llm_api_key: str = ""

    data_dir: Path = REPO_ROOT / "data"
    sqlite_path: Path = REPO_ROOT / "data" / "yoyo.db"

    # Obsidian vault. Canon per the handoff: Yoyo reads it, and writes drafts only.
    vault_path: Path | None = None

    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "yoyo_corpus"

    api_host: str = "127.0.0.1"
    # 8081, not 8080: 8080 is the most-collided port on a dev machine and the owner
    # already had something on it. Override with YOYO_API_PORT.
    api_port: int = 8081

    log_level: str = "INFO"
    # Server-side timeout is 900 s and agent tool loops genuinely take minutes.
    # A shorter client timeout just turns a slow success into a spurious failure.
    request_timeout: int = 900


    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


class Role(BaseModel):
    """A Yoyo role, mapped to an endpoint capability name."""

    name: str
    endpoint: str
    tools: bool = False
    reasoning: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    notes: str | None = None

    def check(self) -> None:
        """A tools:true role pointed at a non-tool-reliable capability is a config bug."""
        if self.tools and self.endpoint in NO_TOOLS_ENDPOINTS:
            raise ValueError(
                f"yoyo-models.yaml: role {self.name!r} has tools: true but points at "
                f"{self.endpoint!r}, which is not tool-reliable. It skips available tools and "
                f"fabricates answers. Point tool-using roles at 'agent'."
            )


class Embeddings(BaseModel):
    provider: str = "local"
    local_model: str = "BAAI/bge-base-en-v1.5"
    endpoint: str | None = None
    dimensions: int = 768
    notes: str | None = None


class Reranking(BaseModel):
    enabled: bool = False
    endpoint: str | None = None
    notes: str | None = None


class Retrieval(BaseModel):
    chunk_size: int = 1200
    chunk_overlap: int = 150
    dense_top_k: int = 20
    sparse_top_k: int = 20
    final_top_k: int = 6
    rrf_k: int = 60
    max_context_chars: int = 24000


class Endpoint(BaseModel):
    base_url_env: str = "YOYO_LLM_BASE_URL"
    api_key_env: str = "YOYO_LLM_API_KEY"
    context_ceiling: int = 32768


class ModelConfig(BaseModel):
    endpoint: Endpoint = Field(default_factory=Endpoint)
    roles: dict[str, Role] = Field(default_factory=dict)
    embeddings: Embeddings = Field(default_factory=Embeddings)
    reranking: Reranking = Field(default_factory=Reranking)
    retrieval: Retrieval = Field(default_factory=Retrieval)

    def role(self, name: str) -> Role:
        try:
            r = self.roles[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.roles)) or "<none>"
            raise KeyError(f"No role {name!r} in yoyo-models.yaml. Known: {known}") from exc
        r.check()
        return r


def load_model_config(path: Path | None = None) -> ModelConfig:
    path = path or MODELS_FILE
    if not path.exists():
        raise FileNotFoundError(f"Missing model config: {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    roles: dict[str, Role] = {}
    for name, body in (raw.get("roles") or {}).items():
        role = Role(name=name, **(body or {}))
        role.check()  # fail loudly at load time, not at the first tool call
        roles[name] = role

    return ModelConfig(
        endpoint=Endpoint(**(raw.get("endpoint") or {})),
        roles=roles,
        embeddings=Embeddings(**(raw.get("embeddings") or {})),
        reranking=Reranking(**(raw.get("reranking") or {})),
        retrieval=Retrieval(**(raw.get("retrieval") or {})),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


@lru_cache(maxsize=1)
def get_models() -> ModelConfig:
    return load_model_config()


def redact(value: str) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}"


def env_summary() -> dict[str, str]:
    s = get_settings()
    return {
        "llm_base_url": s.llm_base_url,
        "llm_api_key": redact(s.llm_api_key),
        "request_timeout": str(s.request_timeout),
        "sqlite_path": str(s.sqlite_path),
        "qdrant_url": s.qdrant_url,
        "qdrant_collection": s.qdrant_collection,
        "models_file": str(MODELS_FILE),
        "cwd": os.getcwd(),
    }
