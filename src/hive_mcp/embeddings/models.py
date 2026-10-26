"""Pinned local embedding models: where to get them, how to verify them, where they live."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileSpec:
    """A model that is two pinned files: the ONNX network and its tokenizer."""

    name: str
    model_url: str
    model_sha256: str
    tokenizer_url: str
    tokenizer_sha256: str


@dataclass(frozen=True, slots=True)
class RerankSpec(FileSpec):
    min_score: float  # cross-encoder logit below which a candidate counts as unrelated
    max_tokens: int = 384


@dataclass(frozen=True, slots=True)
class ModelSpec(FileSpec):
    dim: int
    query_prefix: str  # retrieval models expect this before queries...
    min_similarity: float  # calibrated on real text, see docs/EMBEDDINGS.md
    max_tokens: int = 256
    pooling: str = "cls"  # how token vectors become one: "cls" (first token) or "mean"
    doc_prefix: str = ""  # ...and some also before documents ("passage: " for e5)
    languages: str = "English"

    @property
    def model_id(self) -> str:
        # Anything that changes the vectors must change this, or old and new would be compared.
        return f"onnx-{self.name}-{self.pooling}-t{self.max_tokens}"


BGE_SMALL = ModelSpec(
    name="bge-small-en-v1.5-int8",
    dim=384,
    model_url="https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q/resolve/main/model_optimized.onnx",
    model_sha256="51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
    tokenizer_url="https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/main/tokenizer.json",
    tokenizer_sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    query_prefix="Represent this sentence for searching relevant passages: ",
    min_similarity=0.58,  # evals/core.json: no unrelated query passes, R@3 0.97 (0.98 at 0.55)
)

E5_SMALL = ModelSpec(
    name="multilingual-e5-small-int8",
    dim=384,
    model_url="https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/onnx/model_quantized.onnx",
    model_sha256="f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
    tokenizer_url="https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/tokenizer.json",
    tokenizer_sha256="0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    query_prefix="query: ",
    doc_prefix="passage: ",
    pooling="mean",
    min_similarity=0.81,  # evals/core + multilingual: R@3 0.92, 17% of unrelated queries pass
    languages="about 100 languages",
)

SPECS = {s.name: s for s in (BGE_SMALL, E5_SMALL)}


MINILM_RERANKER = RerankSpec(
    name="ms-marco-minilm-l6-int8",
    model_url="https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/resolve/main/onnx/model_quantized.onnx",
    model_sha256="e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe",
    tokenizer_url="https://huggingface.co/Xenova/ms-marco-MiniLM-L-6-v2/resolve/main/tokenizer.json",
    tokenizer_sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    min_score=-100.0,  # placeholder until calibrated with evals/
)
RERANKERS = {MINILM_RERANKER.name: MINILM_RERANKER}


def _lookup[T](name: str, table: Mapping[str, T], what: str) -> T:
    try:
        return table[name]
    except KeyError:
        raise ValidationError(f"unknown {what} {name!r} (available: {', '.join(table)})") from None


def get_spec(name: str) -> ModelSpec:
    return _lookup(name, SPECS, "model")


def get_rerank_spec(name: str) -> RerankSpec:
    return _lookup(name, RERANKERS, "reranker")


def find_files(name: str) -> FileSpec:
    """Any downloadable model by name (for `hive models fetch`)."""
    return _lookup(name, {**SPECS, **RERANKERS}, "model")


def model_files(spec: FileSpec, root: Path) -> tuple[Path, Path]:
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


def fetch_model(spec: FileSpec, root: Path) -> tuple[Path, Path]:
    """Download (once) and verify the model files; a file with the wrong hash is replaced."""
    model, tokenizer = model_files(spec, root)
    for url, expected, dest in (
        (spec.model_url, spec.model_sha256, model),
        (spec.tokenizer_url, spec.tokenizer_sha256, tokenizer),
    ):
        if not dest.exists() or sha256_of(dest) != expected:
            _download(url, expected, dest)
    return model, tokenizer
