import hashlib
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError
from hive_mcp.embeddings import models
from hive_mcp.embeddings.models import BGE_SMALL, fetch_model, get_spec, model_files
from hive_mcp.embeddings.onnx import OnnxEmbedder

REAL = os.environ.get("HIVE_TEST_MODELS")  # directory holding <model name>/model.onnx
needs_model = pytest.mark.skipif(not REAL, reason="set HIVE_TEST_MODELS to run real-model tests")


def test_unknown_model_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_spec("nope")


def test_model_id_includes_truncation():
    assert replace(BGE_SMALL, max_tokens=128).model_id != BGE_SMALL.model_id


def test_missing_files_say_how_to_fix(tmp_path):
    with pytest.raises(EmbeddingUnavailable, match="hive models fetch"):
        OnnxEmbedder(BGE_SMALL, tmp_path / "a", tmp_path / "b")


def spec_for(url_model, url_tok, model_bytes, tok_bytes):
    return replace(
        BGE_SMALL,
        model_url=url_model,
        tokenizer_url=url_tok,
        model_sha256=hashlib.sha256(model_bytes).hexdigest(),
        tokenizer_sha256=hashlib.sha256(tok_bytes).hexdigest(),
    )


def serve(files, monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=files[str(r.url)]))

    def stream(method, url, **kw):
        kw.pop("timeout", None)
        return httpx.Client(transport=transport).stream(method, url, **kw)

    monkeypatch.setattr(models.httpx, "stream", stream)


def test_fetch_downloads_verifies_and_is_idempotent(tmp_path, monkeypatch):
    files = {"http://m/model": b"model-bytes", "http://m/tok": b"tok-bytes"}
    spec = spec_for("http://m/model", "http://m/tok", *files.values())
    serve(files, monkeypatch)
    model, tok = fetch_model(spec, tmp_path)
    assert (model.read_bytes(), tok.read_bytes()) == (b"model-bytes", b"tok-bytes")
    files.clear()  # a second fetch must not touch the network
    assert fetch_model(spec, tmp_path) == (model, tok)
    assert not list(Path(tmp_path).rglob("*.part"))


def test_fetch_rejects_tampered_download_and_leaves_nothing(tmp_path, monkeypatch):
    files = {"http://m/model": b"evil", "http://m/tok": b"tok"}
    spec = spec_for("http://m/model", "http://m/tok", b"model-bytes", b"tok")
    serve(files, monkeypatch)
    with pytest.raises(EmbeddingUnavailable, match="checksum"):
        fetch_model(spec, tmp_path)
    model, _ = model_files(spec, tmp_path)
    assert not model.exists() and not list(tmp_path.rglob("*.part"))


def test_fetch_replaces_a_corrupt_existing_file(tmp_path, monkeypatch):
    files = {"http://m/model": b"model-bytes", "http://m/tok": b"tok"}
    spec = spec_for("http://m/model", "http://m/tok", *files.values())
    model, _ = model_files(spec, tmp_path)
    model.parent.mkdir(parents=True)
    model.write_bytes(b"truncated")
    serve(files, monkeypatch)
    fetch_model(spec, tmp_path)
    assert model.read_bytes() == b"model-bytes"


@needs_model
async def test_real_model_understands_paraphrases():
    model, tok = model_files(BGE_SMALL, Path(REAL))
    emb = OnnxEmbedder(BGE_SMALL, model, tok)
    docs = await emb.embed(
        [
            "Radar measures range by timing the delay of an echo.",
            "To bake a chocolate cake, cream butter and sugar.",
        ]
    )
    q = await emb.embed_query("how is distance found from a pulse echo?")
    assert float(q @ docs[0]) > emb.min_similarity > float(q @ docs[1])
    assert len(docs[0]) == emb.dim
    with pytest.raises(ValidationError):
        await emb.embed([" "])
