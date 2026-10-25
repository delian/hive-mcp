"""Chooses the embedder from settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.embeddings.models import get_spec, model_files
from hive_mcp.embeddings.onnx import OnnxEmbedder
from hive_mcp.embeddings.openai_compat import OpenAICompatEmbedder

if TYPE_CHECKING:
    from hive_mcp.config import Settings
    from hive_mcp.ports.embeddings import Embedder

KINDS = ("hashing", "onnx", "openai")


def embed_min_level(settings: Settings) -> ScopeLevel:
    """Which items may be sent to the embedder (see Settings.embedding_min_scope)."""
    if settings.embedding_min_scope is None:
        return ScopeLevel.PROJECT if settings.embedder == "openai" else ScopeLevel.PRIVATE
    try:
        return ScopeLevel[settings.embedding_min_scope.upper()]
    except KeyError:
        names = ", ".join(level.name.lower() for level in ScopeLevel)
        raise ValidationError(f"HIVE_EMBEDDING_MIN_SCOPE must be one of: {names}") from None


def make_embedder(settings: Settings) -> Embedder:
    kind = settings.embedder
    if kind == "hashing":
        return HashingEmbedder(256)
    if kind == "onnx":
        spec = get_spec(settings.embedding_model)
        model, tokenizer = model_files(spec, settings.models_path)
        return OnnxEmbedder(spec, model, tokenizer)
    if kind == "openai":
        if not (settings.embedding_url and settings.embedding_api_key):
            raise ValidationError(
                "openai embedder needs HIVE_EMBEDDING_URL and HIVE_EMBEDDING_API_KEY"
            )
        return OpenAICompatEmbedder(
            settings.embedding_url,
            settings.embedding_model,
            settings.embedding_dim,
            settings.embedding_api_key.get_secret_value(),
            azure=settings.embedding_azure,
            min_similarity=settings.embedding_min_similarity,
        )
    raise ValidationError(f"unknown embedder {kind!r} (available: {', '.join(KINDS)})")
