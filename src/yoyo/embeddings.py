"""Embeddings, routed by yoyo-models.yaml.

MyAIServer does **not** expose an embedding model. bge-m3 and bge-reranker-v2-m3 are named
in the plan but are not deployed. Until they are, embeddings run locally on the laptop
(CPU, fastembed) — sanctioned by the handoff.

Flipping to the server later is a yaml edit (`provider: server`) plus a full reindex,
because changing the embedding model invalidates every vector in the corpus.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Iterable

from .config import get_models, get_settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _local_model():  # noqa: ANN202
    cfg = get_models().embeddings

    # Windows has no symlinks without Developer Mode; the warning is noise, not a problem.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    # fastembed defaults to %TEMP%, which Windows cleans — that silently re-downloads
    # ~220 MB. Keep the cache with the rest of Yoyo's data.
    cache_dir = get_settings().data_dir / "models"
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise EmbeddingError(
            "Local embeddings need fastembed. Install with: uv pip install -e \".[local-embed]\"\n"
            "Or set embeddings.provider: server in yoyo-models.yaml once MyAIServer exposes one."
        ) from exc

    log.info("loading %s (cache: %s)", cfg.local_model, cache_dir)
    return TextEmbedding(model_name=cfg.local_model, cache_dir=str(cache_dir))


def dimensions() -> int:
    return get_models().embeddings.dimensions


def provider() -> str:
    return get_models().embeddings.provider


def embed(texts: Iterable[str]) -> list[list[float]]:
    batch = list(texts)
    if not batch:
        return []

    cfg = get_models().embeddings
    if cfg.provider == "local":
        vectors = [list(map(float, v)) for v in _local_model().embed(batch)]
    elif cfg.provider == "server":
        vectors = _embed_server(batch)
    else:
        raise EmbeddingError(
            f"embeddings.provider must be 'local' or 'server', got {cfg.provider!r}"
        )

    if vectors and len(vectors[0]) != cfg.dimensions:
        raise EmbeddingError(
            f"Embedding dimension mismatch: yoyo-models.yaml says {cfg.dimensions}, model "
            f"returned {len(vectors[0])}. Fix the yaml, then `yoyo reindex --recreate` — "
            f"existing vectors are unusable."
        )
    return vectors


def _embed_server(batch: list[str]) -> list[list[float]]:
    from .config import get_settings
    from .llm import _client  # noqa: PLC2701 - deliberate single point of egress

    cfg = get_models().embeddings
    if not cfg.endpoint:
        raise EmbeddingError(
            "embeddings.provider is 'server' but embeddings.endpoint is unset in "
            "yoyo-models.yaml. Register an embedding model in LiteLLM, grant this "
            "laptop's key access to it, and put its name here."
        )
    try:
        resp = _client().embeddings.create(model=cfg.endpoint, input=batch)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(
            f"server embeddings failed against {get_settings().llm_base_url} "
            f"(model {cfg.endpoint!r}): {exc}"
        ) from exc
    return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
