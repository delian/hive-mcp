"""Hosted embedder for any OpenAI-compatible `/embeddings` endpoint (OpenAI, Azure OpenAI, vLLM...).

NOTE: item text leaves the server for the provider. Use only where policy allows.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray

BATCH = 64
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class OpenAICompatEmbedder:
    def __init__(  # noqa: PLR0913 - plain configuration
        self,
        url: str,
        model: str,
        dim: int,
        api_key: str,
        *,
        azure: bool = False,
        min_similarity: float = 0.3,
        query_prefix: str = "",
        retries: int = 3,
        backoff: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url, self._model, self._prefix = url, model, query_prefix
        self._headers = {"api-key": api_key} if azure else {"Authorization": f"Bearer {api_key}"}
        self._retries, self._backoff = retries, backoff
        self._client = client or httpx.AsyncClient(timeout=30)
        self.dim, self.min_similarity = dim, min_similarity
        self.model_id = f"openai-{model}-{dim}"

    async def _post(self, batch: list[str]) -> list[Any]:
        body = {"model": self._model, "input": batch, "dimensions": self.dim}
        last = "no attempt"
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            try:
                resp = await self._client.post(self._url, json=body, headers=self._headers)
            except httpx.TransportError as exc:
                last = f"transport error: {exc.__class__.__name__}"
                continue
            if resp.status_code in RETRY_STATUS:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != httpx.codes.OK:
                raise EmbeddingUnavailable(
                    f"embedding provider rejected the request: HTTP {resp.status_code}"
                )
            try:
                data = resp.json()["data"]
                return [row["embedding"] for row in sorted(data, key=lambda r: r["index"])]
            except (ValueError, KeyError, TypeError) as exc:
                raise EmbeddingUnavailable("malformed embedding response") from exc
        raise EmbeddingUnavailable(f"embedding provider unavailable ({last})")

    def _check(self, raw: object) -> NDArray[np.float32]:
        if not isinstance(raw, list) or len(raw) != self.dim:
            raise EmbeddingUnavailable(
                f"provider returned a vector of the wrong size (want {self.dim})"
            )
        try:
            vec = np.asarray(raw, dtype=np.float32)
        except (ValueError, TypeError) as exc:
            raise EmbeddingUnavailable("provider returned a non-numeric vector") from exc
        norm = float(np.linalg.norm(vec))
        if not math.isfinite(norm) or norm == 0.0:
            raise EmbeddingUnavailable("provider returned a zero or non-finite vector")
        return vec / norm

    async def embed(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        for text in texts:
            if not text.strip():
                raise ValidationError("cannot embed empty text")
        out: list[NDArray[np.float32]] = []
        items = list(texts)
        for start in range(0, len(items), BATCH):
            batch = items[start : start + BATCH]
            rows = await self._post(batch)
            if len(rows) != len(batch):
                raise EmbeddingUnavailable("provider returned the wrong number of vectors")
            out.extend(self._check(r) for r in rows)
        return out

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        return (await self.embed([self._prefix + text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()
