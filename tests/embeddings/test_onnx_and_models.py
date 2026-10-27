import hashlib
import os
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest

from hive_mcp.domain.errors import EmbeddingUnavailable, ValidationError
from hive_mcp.embeddings import models
from hive_mcp.embeddings.models import BGE_SMALL, E5_SMALL, fetch_model, get_spec, model_files
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


class FakeEncoding:
    def __init__(self, text):
        n = len(text.split())
        self.ids = list(range(n)) + [0] * (6 - n)  # padded to 6 tokens
        self.attention_mask = [1] * n + [0] * (6 - n)


class FakeTokenizer:
    def __init__(self):
        self.seen = []

    def encode_batch(self, texts):
        self.seen += list(texts)
        return [FakeEncoding(t) for t in texts]


class FakeSession:
    """Hidden states: token i of every text is the vector [i, 1]; padding tokens are huge (must be ignored)."""

    def run(self, _names, feed):
        mask = feed["attention_mask"]
        hidden = np.zeros((mask.shape[0], mask.shape[1], 2), dtype=np.float32)
        for b in range(mask.shape[0]):
            for i in range(mask.shape[1]):
                hidden[b, i] = [i, 1] if mask[b, i] else [1000, -1000]
        return [hidden]


def fake_embedder(spec):
    emb = object.__new__(OnnxEmbedder)
    emb._session, emb._tok = FakeSession(), FakeTokenizer()
    emb._input_names = {"input_ids", "attention_mask"}
    emb._prefix, emb._doc_prefix, emb._pooling = spec.query_prefix, spec.doc_prefix, spec.pooling
    emb.dim, emb.model_id, emb.min_similarity = spec.dim, spec.model_id, spec.min_similarity
    return emb


async def test_mean_pooling_ignores_padding_and_cls_takes_the_first_token():
    mean = fake_embedder(replace(E5_SMALL, query_prefix="", doc_prefix=""))
    (vec,) = await mean.embed(["a b c"])  # real tokens 0,1,2 -> mean [1, 1] -> unit vector
    assert vec == pytest.approx(np.array([1, 1]) / np.sqrt(2), abs=1e-6)
    cls = fake_embedder(replace(BGE_SMALL, query_prefix="", doc_prefix=""))
    (first,) = await cls.embed(["a b c"])  # token 0 is [0, 1]
    assert first == pytest.approx(np.array([0, 1]), abs=1e-6)


async def test_prefixes_go_on_queries_and_documents_but_never_hide_empty_text():
    emb = fake_embedder(E5_SMALL)
    await emb.embed(["some doc"])
    await emb.embed_query("a question")
    assert emb._tok.seen == ["passage: some doc", "query: a question"]
    for call in (lambda: emb.embed([" "]), lambda: emb.embed_query("")):
        with pytest.raises(ValidationError):
            await call()
    assert len(emb._tok.seen) == 2  # nothing was sent to the model


def test_model_ids_differ_when_vectors_would_differ():
    assert E5_SMALL.model_id != BGE_SMALL.model_id
    assert replace(E5_SMALL, pooling="cls").model_id != E5_SMALL.model_id
    assert get_spec("multilingual-e5-small-int8") is E5_SMALL


async def test_onnx_reranker_scores_pairs_in_order_and_batches(monkeypatch):
    from hive_mcp.embeddings.models import MINILM_RERANKER
    from hive_mcp.embeddings.rerank import BATCH, OnnxReranker

    class PairTokenizer:
        def __init__(self):
            self.batches = []

        def encode_batch(self, pairs):
            self.batches.append(list(pairs))
            return [
                type("E", (), {"ids": [1, 2], "attention_mask": [1, 1], "type_ids": [0, 1]})()
                for _ in pairs
            ]

    class Session:
        def run(self, _n, feed):
            return [np.array([[float(i)] for i in range(feed["input_ids"].shape[0])])]

    rr = object.__new__(OnnxReranker)
    rr._session, rr._tok, rr._inputs = (
        Session(),
        PairTokenizer(),
        {"input_ids", "attention_mask", "token_type_ids"},
    )
    rr.model_id, rr.min_score = "x", MINILM_RERANKER.min_score
    assert await rr.score("q", []) == []
    scores = await rr.score("q", [f"p{i}" for i in range(BATCH + 3)])
    assert len(scores) == BATCH + 3 and [len(b) for b in rr._tok.batches] == [BATCH, 3]
    assert all(
        pair[0] == "q" for b in rr._tok.batches for pair in b
    )  # query paired with every passage
