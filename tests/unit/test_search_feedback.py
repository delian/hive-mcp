"""Search feedback: recording, pairing searches with opens, building sets, choosing a floor."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from hive_mcp.bootstrap import build_services
from hive_mcp.cli import cli
from hive_mcp.config import Settings
from hive_mcp.embeddings.factory import make_embedder
from hive_mcp.evaluation import dataset
from hive_mcp.evaluation.feedback import build_set, pair_searches_with_opens
from hive_mcp.evaluation.runner import FloorPoint, recommend_floor
from hive_mcp.ports.audit import AuditEvent, AuditRecord
from hive_mcp.services.feedback import OPEN, QUERY
from tests.conformance.factory import ALICE

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def search(seq, minute, actor, text, hits):
    event = AuditEvent(
        "o", actor, QUERY, T0 + timedelta(minutes=minute), None, {"q": text, "hits": ",".join(hits)}
    )
    return AuditRecord(seq, event)


def opened(seq, minute, actor, item):
    return AuditRecord(seq, AuditEvent("o", actor, OPEN, T0 + timedelta(minutes=minute), item, {}))


# ---------------------------------------------------------------- pairing
def test_an_open_is_credited_to_the_search_that_showed_it():
    pairs = pair_searches_with_opens(
        [
            search(1, 0, "ann", "radar range", ["a", "b"]),
            opened(2, 1, "ann", "b"),
            opened(3, 2, "ann", "a"),
        ]
    )
    assert pairs.relevant == {"radar range": {"a", "b"}} and (pairs.searches, pairs.opens) == (1, 2)


def test_opens_of_things_the_search_did_not_show_or_by_someone_else_or_too_late_do_not_count():
    pairs = pair_searches_with_opens(
        [
            search(1, 0, "ann", "radar", ["a"]),
            opened(2, 1, "ann", "zzz"),  # not among the results (found some other way)
            opened(3, 1, "bob", "a"),  # a different person
            opened(4, 31, "ann", "a"),  # half an hour later: no longer about that search
        ]
    )
    assert pairs.relevant == {} and pairs.opens == 3


def test_the_latest_matching_search_gets_the_credit_and_each_person_is_separate():
    pairs = pair_searches_with_opens(
        [
            search(1, 0, "ann", "first try", ["a"]),
            search(2, 2, "ann", "better words", ["a", "b"]),
            search(3, 3, "bob", "bob's search", ["a"]),
            opened(4, 4, "ann", "a"),
            opened(5, 5, "bob", "a"),
        ]
    )
    assert pairs.relevant == {"better words": {"a"}, "bob's search": {"a"}}


def test_old_searches_fall_out_of_a_persons_memory():
    records = [search(1, 0, "ann", "old", ["x"])]
    records += [search(10 + i, 1, "ann", f"s{i}", ["y"]) for i in range(10)]
    records.append(opened(99, 2, "ann", "x"))
    assert pair_searches_with_opens(records).relevant == {}


def test_malformed_events_are_ignored():
    bad = AuditRecord(1, AuditEvent("o", "ann", QUERY, T0, None, {}))
    assert (
        pair_searches_with_opens(
            [bad, AuditRecord(2, AuditEvent("o", "ann", OPEN, T0, None, {}))]
        ).opens
        == 0
    )


# ---------------------------------------------------------------- building a set
def test_the_set_has_labelled_queries_negatives_and_noise_but_only_for_items_that_exist():
    pairs = pair_searches_with_opens(
        [search(1, 0, "ann", "q1", ["a", "gone"]), opened(2, 1, "ann", "a"), opened(3, 1, "ann", "gone"),
         search(4, 2, "ann", "q2", ["gone"]), opened(5, 3, "ann", "gone")]
    )  # fmt: skip
    built = build_set(
        "t", pairs, {"a": ("Title A", "Body A")},
        distractors=[("a", "dup", "dup"), ("n1", "N1", "x"), ("n2", "N2", "y")], negatives=["weather in paris"],
    )  # fmt: skip
    assert [q for q in built["queries"] if q["relevant"]] == [
        {"q": "q1", "relevant": ["a"], "type": "feedback"}
    ]
    assert built["queries"][-1] == {"q": "weather in paris", "relevant": [], "type": "negative"}
    assert sorted(d["id"] for d in built["docs"]) == [
        "a",
        "n1",
        "n2",
    ]  # the relevant doc is not duplicated
    assert next(d for d in built["docs"] if d["id"] == "a")["title"] == "Title A"
    data = dataset.parse(built)  # and `hive eval` accepts it
    assert len(data.queries) == 2


# ---------------------------------------------------------------- floor recommendation
def test_the_recommended_floor_is_the_best_recall_among_those_that_stay_quiet_on_unrelated_queries():
    pts = [FloorPoint(0.3, 0.99, 0.40), FloorPoint(0.5, 0.95, 0.04), FloorPoint(0.6, 0.95, 0.01),
           FloorPoint(0.7, 0.80, 0.0)]  # fmt: skip
    assert recommend_floor(pts).floor == 0.6  # same recall as 0.5, stricter
    assert recommend_floor(pts, max_false_positives=0.5).floor == 0.3
    assert recommend_floor([FloorPoint(0.3, 0.9, 0.5)]) is None and recommend_floor([]) is None


def test_the_floor_can_be_overridden_for_any_embedder():
    assert make_embedder(Settings(embedder="hashing", relevance_floor=0.42)).min_similarity == 0.42
    assert make_embedder(Settings(embedder="hashing")).min_similarity == 0.25


# ---------------------------------------------------------------- recording and CLI
async def test_recording_redacts_and_shortens_queries(tmp_path):
    services = await build_services(Settings(data_dir=tmp_path, search_feedback=True))
    try:
        await services.feedback.queried(
            ALICE, "why does AKIAIOSFODNN7EXAMPLE fail " + "x" * 500, [f"i{n}" for n in range(40)]
        )
        (row,) = await services.backend.audit.list("org1", action=QUERY)
        assert "AKIA" not in row.event.details["q"] and len(row.event.details["q"]) <= 200
        assert row.event.details["hits"].count(",") == 24  # 25 ids at most
    finally:
        await services.close()


async def test_feedback_is_off_unless_enabled(tmp_path):
    services = await build_services(Settings(data_dir=tmp_path))
    try:
        assert services.feedback is None
    finally:
        await services.close()


async def populate(settings: Settings) -> str:
    services = await build_services(settings)
    try:
        from hive_mcp.services.models import RememberRequest

        made = await services.knowledge.remember(
            ALICE, RememberRequest(title="Radar range", body="Range follows from pulse delay.")
        )
        for i in range(3):
            await services.knowledge.remember(
                ALICE, RememberRequest(title=f"Other {i}", body=f"Unrelated cooking note {i}.")
            )
        item_id = made.item.id
        await services.feedback.queried(ALICE, "how far away is the target", [item_id])
        await services.feedback.opened(ALICE, item_id)
    finally:
        await services.close()
    return item_id


def test_eval_build_writes_a_private_set_that_hive_eval_accepts(tmp_path, monkeypatch):
    item_id = asyncio.run(populate(Settings(data_dir=tmp_path / "data", search_feedback=True)))
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    out = tmp_path / "real.json"
    result = CliRunner().invoke(
        cli, ["eval-build", str(out), "--org", "org1", "--distractors", "10"]
    )
    assert result.exit_code == 0, result.output
    built = json.loads(out.read_text())
    assert built["queries"] == [
        {"q": "how far away is the target", "relevant": [item_id], "type": "feedback"}
    ]
    assert len(built["docs"]) == 4  # the relevant item and the three others as noise
    assert (out.stat().st_mode & 0o777) == 0o600
    assert "keep it private" in result.output
    assert dataset.load(out).queries[0].relevant == frozenset({item_id})


# ---------------------------------------------------------------- explicit ratings and the report
def rating(seq, minute, actor, item, useful, q=None):  # noqa: PLR0913, PLR0917
    from hive_mcp.services.feedback import RATING

    details = {"useful": "1" if useful else "0", **({"q": q} if q else {})}
    return AuditRecord(
        seq, AuditEvent("o", actor, RATING, T0 + timedelta(minutes=minute), item, details)
    )


def test_ratings_label_results_and_a_no_outweighs_an_open():
    pairs = pair_searches_with_opens(
        [
            search(1, 0, "ann", "radar range", ["a", "b", "c"]),
            opened(2, 1, "ann", "a"),
            opened(3, 1, "ann", "b"),
            rating(4, 2, "ann", "a", useful=False),  # opened it, then found it wrong
            rating(5, 2, "ann", "c", useful=True),  # credited to her latest search showing c
        ]
    )
    assert pairs.relevant == {"radar range": {"b", "c"}}
    assert pairs.irrelevant == {"radar range": {"a"}}
    assert (pairs.rated_useful, pairs.rated_useless) == (1, 1)


def test_a_rating_may_name_its_query_and_a_no_before_the_open_still_wins():
    pairs = pair_searches_with_opens(
        [
            rating(1, 0, "ann", "a", useful=False, q="old question"),
            search(2, 5, "ann", "old question", ["a"]),
            opened(3, 6, "ann", "a"),
            rating(4, 7, "bob", "x", useful=True, q="other question"),
        ]
    )
    assert pairs.relevant == {"other question": {"x"}}
    assert pairs.irrelevant == {"old question": {"a"}}


def test_the_report_lists_gaps_empty_searches_and_winners():
    from hive_mcp.evaluation.report import build_report

    report = build_report(
        [
            search(1, 0, "ann", "radar range", ["a"]),
            opened(2, 1, "ann", "a"),
            search(3, 2, "ann", "vpn setup", ["z"]),  # results, nobody used them
            search(4, 3, "bob", "vpn setup", ["z"]),
            search(5, 4, "bob", "wifi password", []),  # nothing found
            search(6, 5, "bob", "radar range", ["a", "b"]),
            rating(7, 6, "bob", "b", useful=False),
        ]
    )
    assert report.searches == 5
    assert report.answered == 2  # by search text: both "radar range" searches
    assert report.gaps == [("vpn setup", 2)]
    assert report.empty == [("wifi password", 1)]
    assert report.useful == [("a", 1)] and report.useless == [("b", 1)]
