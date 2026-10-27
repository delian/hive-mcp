"""The measuring tools for the curation helpers, with a scripted model."""

import json
from pathlib import Path

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.evaluation import dataset
from hive_mcp.evaluation.assist_eval import (
    evaluate_conflicts,
    evaluate_merges,
    replay_votes,
)
from hive_mcp.llm.providers import LlmError

EVALS = Path(__file__).parents[2] / "evals"


class Scripted:
    """A model that answers contradiction questions from a table and merges by concatenating."""

    def __init__(self, contradict: bool = True, fail: bool = False) -> None:
        self.contradict, self.fail = contradict, fail

    async def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        if self.fail:
            raise LlmError("down")
        if "contradict" in system:
            return json.dumps({"contradict": self.contradict, "why": "scripted"})
        bodies = " ".join(part.split("\n", 2)[-1] for part in user.split("<note")[1:])
        return json.dumps({"title": "Merged", "body": bodies})


async def test_the_heuristic_is_scored_on_the_shipped_conflict_set():
    rows = await evaluate_conflicts(json.loads((EVALS / "conflicts.json").read_text()), None)
    assert [r.name for r in rows] == ["heuristic"]
    h = rows[0]
    assert h.tp + h.fp + h.fn + h.tn == 24
    assert 0.5 < h.precision < 0.7 and 0.6 < h.recall < 0.75  # the figures docs/LLM.md quotes


async def test_a_model_is_scored_alone_and_combined_with_the_heuristic():
    raw = json.loads((EVALS / "conflicts.json").read_text())
    always = {r.name: r for r in await evaluate_conflicts(raw, Scripted(True))}
    assert always["model"].recall == 1.0 and always["model"].precision == pytest.approx(0.5)
    never = {r.name: r for r in await evaluate_conflicts(raw, Scripted(False))}
    assert never["model"].tp == 0 and never["model"].fp == 0
    broken = {r.name: r for r in await evaluate_conflicts(raw, Scripted(fail=True))}
    # a failing model falls back to the heuristic in the pipeline as shipped
    heuristic = (await evaluate_conflicts(raw, None))[0]
    assert broken["model, else heuristic"].tp == heuristic.tp


async def test_a_malformed_conflict_set_is_refused():
    with pytest.raises(ValidationError):
        await evaluate_conflicts({"pairs": []}, None)


async def test_merge_scores_count_the_facts_that_survive():
    raw = json.loads((EVALS / "merges.json").read_text())
    scores = await evaluate_merges(raw, Scripted())
    assert all(s.kept == s.total for s in scores)  # concatenation keeps everything
    failed = await evaluate_merges(raw, Scripted(fail=True))
    assert all(s.kept == 0 and s.error for s in failed)
    lossy = {"groups": [{"notes": [{"title": "a", "body": "alpha beta"}], "facts": ["gamma"]}]}
    assert (await evaluate_merges(lossy, Scripted()))[0].missing == ["gamma"]


async def test_the_vote_replay_reports_a_baseline_each_accuracy_and_an_attack():
    ds = dataset.parse(json.loads((EVALS / "core.json").read_text()))
    points = await replay_votes(ds, HashingEmbedder(), accuracies=(1.0, 0.5), voters=2)
    labels = [p.label for p in points]
    assert labels == [
        "no nudges",
        "votes 100% right",
        "votes 50% right",
        "40 votes for one wrong note",
    ]
    base = points[0]
    # the nudge is bounded, so even forty votes for a wrong note cannot move results much
    assert all(abs(p.mrr - base.mrr) < 0.06 for p in points)
