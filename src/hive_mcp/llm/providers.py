"""Two ways to reach a model: an OpenAI-compatible chat endpoint (also Azure OpenAI), and Anthropic.

The prompts carry other people's notes, so they are data, never instructions to the server: the
caller delimits them and only ever uses the reply as a *proposal* a person reviews.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from hive_mcp.domain.errors import HiveError

TIMEOUT = 60.0
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class LlmError(HiveError):
    """The model service failed or answered with something unusable."""


class LlmProvider(Protocol):
    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str: ...


class OpenAICompatLlm:
    def __init__(
        self,
        url: str,
        model: str,
        api_key: str,
        *,
        azure: bool = False,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url, self._model, self._key, self._azure = url, model, api_key, azure
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT)

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        headers = (
            {"api-key": self._key} if self._azure else {"Authorization": f"Bearer {self._key}"}
        )
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        try:
            response = await self._http.post(self._url, headers=headers, json=body)
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError("the language model service failed") from exc


class AnthropicLlm:
    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        url: str = ANTHROPIC_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url, self._model, self._key = url, model, api_key
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT)

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION}
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = await self._http.post(self._url, headers=headers, json=body)
            response.raise_for_status()
            blocks = response.json()["content"]
            return "".join(str(b["text"]) for b in blocks if b.get("type") == "text")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LlmError("the language model service failed") from exc
