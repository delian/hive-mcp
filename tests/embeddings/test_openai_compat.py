import json

import httpx
import numpy as np
import pytest

from hive_mcp.config import Settings
from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.embeddings.factory import embed_min_level
from hive_mcp.embeddings.openai_compat import OpenAICompatEmbedder

DIM = 8


def make(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatEmbedder(
        "http://x/embeddings", "m", DIM, "k", backoff=0, client=client, **kw
    )


def ok(request, scale=1.0):
    inputs = json.loads(request.content)["input"]
    rows = [{"index": i, "embedding": [scale] * DIM} for i in range(len(inputs))]
    return httpx.Response(200, json={"data": list(reversed(rows))})  # out of order on purpose


async def test_embeds_in_input_order_and_normalises():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request, 3.0)

    vecs = await make(handler).embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(abs(float(np.linalg.norm(v)) - 1) < 1e-5 for v in vecs)
    assert seen[0].headers["authorization"] == "Bearer k"


async def test_batches_large_inputs():
    calls = []

    def handler(request):
        calls.append(1)
        return ok(request)

    assert len(await make(handler).embed([f"t{i}" for i in range(150)])) == 150
    assert len(calls) == 3


async def test_azure_uses_api_key_header():
    seen = []

    def handler(request):
        seen.append(request.headers)
        return ok(request)

    await make(handler, azure=True).embed_query("q")
    assert seen[0]["api-key"] == "k" and "authorization" not in seen[0]


async def test_retries_transient_errors_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else ok(request)

    assert len(await make(handler).embed(["a"])) == 1
    assert len(attempts) == 3


async def test_gives_up_after_retries_and_does_not_retry_client_errors():
    n = []

    def down(request):
        n.append(1)
        return httpx.Response(500)

    with pytest.raises(EmbeddingUnavailable):
        await make(down).embed(["a"])
    assert len(n) == 4
    n.clear()

    def denied(request):
        n.append(1)
        return httpx.Response(401)

    with pytest.raises(EmbeddingUnavailable):
        await make(denied).embed(["a"])
    assert len(n) == 1


async def test_transport_errors_are_unavailable():
    def boom(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(EmbeddingUnavailable):
        await make(boom).embed(["a"])


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "embedding": [1.0] * (DIM - 1)}]},  # wrong size
        {"data": [{"index": 0, "embedding": [0.0] * DIM}]},  # zero
        {"data": [{"index": 0, "embedding": ["x"] * DIM}]},  # not numbers
        {"data": []},  # wrong count
        {"nope": 1},
        {"data": [{"index": 0, "embedding": [1e39] * DIM}]},  # overflows float32 to inf
    ],
)
async def test_bad_provider_answers_are_rejected(payload):
    with pytest.raises(EmbeddingUnavailable):
        await make(lambda r: httpx.Response(200, json=payload)).embed(["a"])


async def test_rejects_empty_text_without_calling_the_provider():
    def handler(request):
        raise AssertionError("must not be called")

    with pytest.raises(ValidationError):
        await make(handler).embed(["  "])


def test_hosted_embedders_default_to_keeping_private_notes_local():
    assert embed_min_level(Settings(embedder="hashing")) is ScopeLevel.PRIVATE
    assert embed_min_level(Settings(embedder="onnx")) is ScopeLevel.PRIVATE
    assert embed_min_level(Settings(embedder="openai")) is ScopeLevel.PROJECT
    assert (
        embed_min_level(Settings(embedder="openai", embedding_min_scope="Team")) is ScopeLevel.TEAM
    )
    assert (
        embed_min_level(Settings(embedder="openai", embedding_min_scope="private"))
        is ScopeLevel.PRIVATE
    )
    with pytest.raises(ValidationError):
        embed_min_level(Settings(embedder="openai", embedding_min_scope="galaxy"))
