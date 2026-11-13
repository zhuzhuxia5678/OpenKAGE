"""Embedding client for any OpenAI-compatible ``/embeddings`` endpoint.

Environment (or ``.env`` at the repository root, see ``.env.example``):
  EMBEDDING_API_KEY      key for the embeddings endpoint. Unset -> the vector channel is
                         off (NullEmbedder) and retrieval falls back to graph structure only.
  EMBEDDING_BASE_URL     endpoint base URL; default https://api.openai.com/v1
  EMBEDDING_MODEL        model id; default text-embedding-3-small
  EMBEDDING_DIM          vector size; default 896, the setting used in the paper.
                         Set it to the dimension your provider actually returns.
  EMBEDDING_BATCH        texts per request (default 64; some providers cap it, e.g. 10)
  EMBEDDING_QUERY_PREFIX optional instruction prefix for the query side of asymmetric models
                         (never applied to indexed text)
  EMBEDDING_MAX_RETRIES / EMBEDDING_RETRY_BASE_DELAY / EMBEDDING_RETRY_MAX_DELAY
                         transport retries (defaults 3 / 0.5 s / 8 s)
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Protocol

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional: plain environment variables work too

    def load_dotenv(*_args, **_kwargs) -> None:  # type: ignore[misc]
        return None


logger = logging.getLogger(__name__)

_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=False)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return max(0, int(_env(key)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return max(0.0, float(_env(key)))
    except ValueError:
        return default


class Embedder(Protocol):
    """``is_query`` supports asymmetric models: queries get the instruction prefix, indexed
    documents do not."""

    dim: int
    model: str

    def embed_one(self, text: str, *, is_query: bool = False) -> list[float]: ...

    def embed_many(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...


class NullEmbedder:
    """Placeholder: every call returns an empty vector; the vector store sees ``dim == 0``
    and turns the vector channel off."""

    dim: int = 0
    model: str = ""

    def embed_one(self, text: str, *, is_query: bool = False) -> list[float]:
        return []

    def embed_many(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        return [[] for _ in texts]


class OpenAIEmbedder:
    """Client for the OpenAI ``/embeddings`` protocol. Any exception propagates: it is
    either a vector or an error, and the caller decides how to degrade."""

    def __init__(self, *, api_key: str, base_url: str | None, model: str, dim: int,
                 query_prefix: str = "", batch_size: int = 64, timeout: float = 60.0,
                 max_retries: int = 3, retry_base_delay: float = 0.5,
                 retry_max_delay: float = 8.0) -> None:
        import openai

        self.model = model
        self.dim = dim
        self._query_prefix = query_prefix
        self.batch_size = batch_size
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                                     max_retries=0)

    def _is_retryable(self, exc: Exception) -> bool:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(exc, (APITimeoutError, APIConnectionError)):
            return True
        return isinstance(exc, APIStatusError) and exc.status_code in (408, 429, 500, 502, 503, 504)

    def _post(self, inputs: list[str]) -> list[list[float]]:
        for attempt in range(1, self._max_retries + 2):
            try:
                resp = self._client.embeddings.create(model=self.model, input=inputs)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt <= self._max_retries and self._is_retryable(exc):
                    delay = min(self._retry_max_delay,
                                self._retry_base_delay * (2 ** (attempt - 1)))
                    logger.warning("embedding call failed (%s), retry in %.1fs", exc, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"embedding request failed after {attempt} attempts: {exc}") from exc
        vecs = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        if len(vecs) != len(inputs):
            raise RuntimeError(f"expected {len(inputs)} embeddings, got {len(vecs)}")
        return [list(v) for v in vecs]

    def _prefixed(self, text: str, is_query: bool) -> str:
        return self._query_prefix + text if is_query and self._query_prefix else text

    def embed_one(self, text: str, *, is_query: bool = False) -> list[float]:
        if not text:
            return []
        return self._post([self._prefixed(text, is_query)])[0]

    def embed_many(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        cleaned = [(i, self._prefixed(t, is_query)) for i, t in enumerate(texts) if t]
        out: dict[int, list[float]] = {}
        for start in range(0, len(cleaned), self.batch_size):
            batch = cleaned[start:start + self.batch_size]
            for (idx, _), vec in zip(batch, self._post([t for _, t in batch])):
                out[idx] = vec
        return [out.get(i, []) for i in range(len(texts))]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Process-wide embedder. Unset ``EMBEDDING_API_KEY`` or a failing startup probe turns
    the vector channel off (NullEmbedder); a dimension mismatch also turns it off rather
    than mixing dimensions."""
    api_key = _env("EMBEDDING_API_KEY")
    if not api_key:
        logger.info("embedder: EMBEDDING_API_KEY not set, vector channel off")
        return NullEmbedder()
    model = _env("EMBEDDING_MODEL", "text-embedding-3-small")
    dim = _env_int("EMBEDDING_DIM", 896)
    try:
        emb = OpenAIEmbedder(
            api_key=api_key, base_url=_env("EMBEDDING_BASE_URL") or None, model=model,
            dim=dim, query_prefix=os.environ.get("EMBEDDING_QUERY_PREFIX", "").strip('"'),
            batch_size=max(1, _env_int("EMBEDDING_BATCH", 64)),
            max_retries=_env_int("EMBEDDING_MAX_RETRIES", 3),
            retry_base_delay=_env_float("EMBEDDING_RETRY_BASE_DELAY", 0.5),
            retry_max_delay=_env_float("EMBEDDING_RETRY_MAX_DELAY", 8.0),
        )
        vec = emb.embed_one("probe", is_query=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedder: startup probe failed (%s), vector channel off", exc)
        return NullEmbedder()
    if len(vec) != dim:
        logger.warning("embedder: probe returned dim=%d, EMBEDDING_DIM=%d; vector channel off",
                       len(vec), dim)
        return NullEmbedder()
    logger.info("embedder: model=%s dim=%d", model, dim)
    return emb
