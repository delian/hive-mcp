import json
from pathlib import Path

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.evaluation import dataset, runner
from hive_mcp.evaluation.metrics import false_positive_rate, first_hit_rank, summarise
from hive_mcp.search.ranking import RankingConfig, fuse, rrf
from hive_mcp.search.tokenize import query_terms

CORE = Path(__file__).parents[2] / "evals" / "core.json"


def test_metrics():
    rel = frozenset({"b"})
    assert first_hit_rank(["a", "b", "c"], rel) == 2 and first_hit_rank(["a"], rel) is None
    s = summarise([(["b"], rel), (["a", "x", "b"], rel), (["a"], rel), ([], rel)])
    assert s.recall == {1: 0.25, 3: 0.5, 5: 0.5, 10: 0.5}
    assert s.mrr == pytest.approx((1 + 1 / 3) / 4) and s.returned_nothing == 0.25
    assert summarise([]).mrr == 0.0
    assert false_positive_rate([[], ["a"], [], []]) == 0.25 and false_positive_rate([]) == 0.0


def test_dataset_validation(tmp_path):
    good = {
        "docs": [{"id": "1", "title": "t", "body": "b"}],
        "queries": [{"q": "x", "relevant": ["1"]}],
    }
    assert len(dataset.parse(good).queries) == 1
    for bad in (
        {},
        {"docs": [], "queries": []},
        {**good, "queries": [{"q": "x", "relevant": ["nope"]}]},
        {**good, "docs": good["docs"] * 2},
        {"docs": [{"id": "1"}], "queries": [{"q": "x"}]},
    ):
        with pytest.raises(ValidationError):
            dataset.parse(bad)
    path = tmp_path / "e.json"
    path.write_text("not json")
    with pytest.raises(ValidationError):
        dataset.load(path)
    path.write_text(json.dumps(good))
    assert dataset.load(path).name == "unnamed"


def test_stopwords_are_dropped_from_queries_unless_nothing_else_is_left():
    assert query_terms("How do we speed up the database?") == ["speed", "database"]
    assert query_terms("the who") == ["the", "who"]  # only stopwords: keep them all
    assert query_terms("to be or not to be") == [
        "to",
        "be",
        "or",
        "not",
        "to",
        "be",
    ]  # all stopwords
    assert query_terms("Größe der Straße") == [
        "grösse",
        "der",
        "strasse",
    ]  # other languages untouched


def test_weighted_fusion_prefers_the_vector_list_and_caps_keyword_depth():
    cfg = RankingConfig(vector_weight=2.0, keyword_depth=2)
    scores = fuse(["k1", "k2", "k3"], ["v1", "k3"], cfg)
    assert "k3" in scores and scores["k3"] == pytest.approx(2 / 62)  # keyword rank 3 ignored
    assert scores["v1"] > scores["k1"]
    assert rrf([["a"], ["a"]], weights=[1.0, 3.0])["a"] == pytest.approx(4 / 61)


async def test_core_set_is_sane_and_hashing_stays_above_a_regression_floor():
    data = dataset.load(CORE)
    assert len(data.docs) >= 40 and {q.kind for q in data.queries} == {
        "paraphrase",
        "keyword",
        "negative",
    }
    report = await runner.evaluate(data, HashingEmbedder(256))
    by = {m.mode: m for m in report.modes}
    assert by["keyword"].positives.recall[5] > 0.7  # keyword search finds the keyword queries
    assert by["hybrid"].positives.recall[10] > 0.75
    text = runner.format_report(report)
    assert "hybrid" in text and "neg-FP" in text
    swept = await runner.evaluate(data, HashingEmbedder(256), sweep=(0.1, 0.9))
    assert [p.floor for p in swept.sweep] == [0.1, 0.9] and swept.sweep[
        1
    ].false_positive_rate == 0.0
    consult = await runner.consult_check(data, HashingEmbedder(256))
    assert consult.positives.recall[10] > 0.7
