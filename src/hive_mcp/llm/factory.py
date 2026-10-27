"""Chooses the model from settings (None when none is configured)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.llm.providers import AnthropicLlm, OpenAICompatLlm

if TYPE_CHECKING:
    from hive_mcp.config import Settings
    from hive_mcp.llm.providers import LlmProvider


def make_llm(settings: Settings) -> LlmProvider | None:
    if settings.llm_provider == "none":
        return None
    if not (settings.llm_api_key and settings.llm_model):
        raise ValidationError("a language model needs HIVE_LLM_API_KEY and HIVE_LLM_MODEL")
    key = settings.llm_api_key.get_secret_value()
    if settings.llm_provider == "anthropic":
        return AnthropicLlm(
            settings.llm_model, key, url=settings.llm_url or "https://api.anthropic.com/v1/messages"
        )
    if not settings.llm_url:
        raise ValidationError("the openai provider needs HIVE_LLM_URL (the chat completions URL)")
    return OpenAICompatLlm(settings.llm_url, settings.llm_model, key, azure=settings.llm_azure)


def llm_min_level(settings: Settings) -> ScopeLevel:
    """Notes narrower than this never leave the server for the model (default: project up)."""
    try:
        return ScopeLevel[settings.llm_min_scope.upper()]
    except KeyError:
        names = ", ".join(level.name.lower() for level in ScopeLevel)
        raise ValidationError(f"HIVE_LLM_MIN_SCOPE must be one of: {names}") from None
