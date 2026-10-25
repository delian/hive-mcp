"""Offline embedder: ONNX transformer encoder + HF tokenizer, CLS pooling, L2-normalised.

Runs on CPU in a worker thread so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import numpy as np

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from numpy.typing import NDArray

    from hive_mcp.embeddings.models import ModelSpec

BATCH = 32


class OnnxEmbedder:
    def __init__(
        self, spec: ModelSpec, model_path: Path, tokenizer_path: Path, threads: int = 0
    ) -> None:
        try:
            import onnxruntime as ort  # noqa: PLC0415 - optional extra
            from tokenizers import Tokenizer  # noqa: PLC0415
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "local embeddings need the 'local-embeddings' extra (onnxruntime, tokenizers)"
            ) from exc
        for path in (model_path, tokenizer_path):
            if not path.is_file():
                raise EmbeddingUnavailable(f"{path} is missing; run `hive models fetch`")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads  # 0 = let onnxruntime decide
        self._session: Any = ort.InferenceSession(
            str(model_path), opts, providers=["CPUExecutionProvider"]
        )
        self._tok = Tokenizer.from_file(str(tokenizer_path))
        self._tok.enable_truncation(max_length=spec.max_tokens)
        self._tok.enable_padding()
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._prefix = spec.query_prefix
        self.dim, self.model_id, self.min_similarity = spec.dim, spec.model_id, spec.min_similarity

    def _encode(self, texts: list[str]) -> list[NDArray[np.float32]]:
        out: list[NDArray[np.float32]] = []
        for start in range(0, len(texts), BATCH):
            enc = self._tok.encode_batch(texts[start : start + BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            feed = {
                "input_ids": ids,
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            }
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            cls = np.asarray(self._session.run(None, feed)[0])[:, 0, :].astype(np.float32)
            norms = np.linalg.norm(cls, axis=1, keepdims=True)
            if not np.all(np.isfinite(cls)) or np.any(norms == 0):
                raise EmbeddingUnavailable("model produced a non-finite or zero vector")
            out.extend(cls / norms)
        return out

    async def _run(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        for text in texts:
            if not text.strip():
                raise ValidationError("cannot embed empty text")
        return await asyncio.to_thread(self._encode, list(texts))

    async def embed(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        return await self._run(texts)

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        return (await self._run([self._prefix + text]))[0]
