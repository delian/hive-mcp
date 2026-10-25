"""Pinned local embedding models: where to get them, how to verify them, where they live."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    dim: int
    model_url: str
    model_sha256: str
    tokenizer_url: str
    tokenizer_sha256: str
    query_prefix: str  # retrieval models expect this before queries, never before documents
    min_similarity: float  # calibrated on real text, see docs/EMBEDDINGS.md
    max_tokens: int = 256

    @property
    def model_id(self) -> str:
        # Anything that changes the vectors must change this, or old and new would be compared.
        return f"onnx-{self.name}-cls-t{self.max_tokens}"


BGE_SMALL = ModelSpec(
    name="bge-small-en-v1.5-int8",
    dim=384,
    model_url="https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q/resolve/main/model_optimized.onnx",
    model_sha256="51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
    tokenizer_url="https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/tokenizer.json",
    tokenizer_sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    query_prefix="Represent this sentence for searching relevant passages: ",
    min_similarity=0.55,  # positives >= 0.57, unrelated <= 0.53 on a small hand-made set
)

SPECS = {BGE_SMALL.name: BGE_SMALL}


def get_spec(name: str) -> ModelSpec:
    try:
        return SPECS[name]
    except KeyError:
        raise ValidationError(f"unknown model {name!r} (available: {', '.join(SPECS)})") from None


def model_files(spec: ModelSpec, root: Path) -> tuple[Path, Path]:
    return root / spec.name / "model.onnx", root / spec.name / "tokenizer.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, expected: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    digest = hashlib.sha256()
    try:
        with (
            httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp,
            tmp.open("wb") as out,
        ):
            resp.raise_for_status()
            for chunk in resp.iter_bytes(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise EmbeddingUnavailable(f"checksum mismatch downloading {url}")
        tmp.replace(dest)  # atomic: a half-written model is never visible
    except httpx.HTTPError as exc:
        raise EmbeddingUnavailable(f"cannot download {url}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def fetch_model(spec: ModelSpec, root: Path) -> tuple[Path, Path]:
    """Download (once) and verify the model files; a file with the wrong hash is replaced."""
    model, tokenizer = model_files(spec, root)
    for url, expected, dest in (
        (spec.model_url, spec.model_sha256, model),
        (spec.tokenizer_url, spec.tokenizer_sha256, tokenizer),
    ):
        if not dest.exists() or sha256_of(dest) != expected:
            _download(url, expected, dest)
    return model, tokenizer
