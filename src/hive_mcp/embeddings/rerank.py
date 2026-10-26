"""Offline cross-encoder reranker (ONNX): scores a query together with each candidate passage."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import numpy as np

from hive_mcp.domain.errors import EmbeddingUnavailable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from hive_mcp.embeddings.models import RerankSpec

BATCH = 16


class OnnxReranker:
    def __init__(self, spec: RerankSpec, model_path: Path, tokenizer_path: Path) -> None:
        try:
            import onnxruntime as ort  # noqa: PLC0415 - optional extra
            from tokenizers import Tokenizer  # noqa: PLC0415
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "the reranker needs the 'local-embeddings' extra (onnxruntime, tokenizers)"
            ) from exc
        for path in (model_path, tokenizer_path):
            if not path.is_file():
                raise EmbeddingUnavailable(
                    f"{path} is missing; run `hive models fetch {spec.name}`"
                )
        self._session: Any = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._tok = Tokenizer.from_file(str(tokenizer_path))
        self._tok.enable_truncation(max_length=spec.max_tokens)
        self._tok.enable_padding()
        self._inputs = {i.name for i in self._session.get_inputs()}
        self.model_id, self.min_score = f"onnx-{spec.name}", spec.min_score

    def _score(self, query: str, passages: list[str]) -> list[float]:
        out: list[float] = []
        for start in range(0, len(passages), BATCH):
            enc = self._tok.encode_batch([(query, p) for p in passages[start : start + BATCH]])
            feed = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            }
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            logits = np.asarray(self._session.run(None, feed)[0], dtype=np.float32).reshape(
                len(enc), -1
            )
            if not np.all(np.isfinite(logits)):
                raise EmbeddingUnavailable("reranker produced a non-finite score")
            out.extend(float(x) for x in logits[:, 0])
        return out

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return await asyncio.to_thread(self._score, query, list(passages))
