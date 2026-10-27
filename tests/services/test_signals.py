import pytest

from hive_mcp.services.consult import ConsultService
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.services.signals import (
    CONFIRM_BONUS,
    VOTE_RANGE,
    RankSignals,
    vote_factor,
)
from hive_mcp.services.staleness import StaleReview
from tests.conformance.factory import ALICE, BOB, P1


def test_the_vote_nudge_is_bounded_symmetric_and_grows_slowly():
    assert vote_factor(0, 0) == 1.0
    assert vote_factor(3, 0) > 1.0 > vote_factor(0, 3)
    assert vote_factor(5, 0) + vote_factor(0, 5) == pytest.approx(2.0)
    assert 1.0 + VOTE_RANGE * 0.99 < vote_factor(1000, 0) <= 1.0 + VOTE_RANGE
    assert vote_factor(1000, 0) == pytest.approx(1.06, abs=1e-6)
    assert vote_factor(1, 0) < vote_factor(2, 0) < vote_factor(10, 0)


@pytest.fixture
def signals(stack):
    return RankSignals(stack.backend.kv, stack.backend.clock)


async def test_one_vote_per_person_and_note_and_a_changed_mind_replaces_it(stack, signals):
    await signals.record_vote("org1", "alice", "n1", True)
    await signals.record_vote("org1", "alice", "n1", True)  # the same vote again: no effect
    await signals.record_vote("org1", "bob", "n1", True)
    assert (await signals.factors(["n1"]))["n1"] == pytest.approx(vote_factor(2, 0))
    await signals.record_vote("org1", "bob", "n1", False)  # bob changes his mind
    assert (await signals.factors(["n1"]))["n1"] == pytest.approx(vote_factor(1, 1))
    assert (await signals.factors(["unknown"]))["unknown"] == 1.0
    await signals.record_vote("org2", "alice", "n1", True)  # another org's alice is another person
    assert (await signals.factors(["n1"]))["n1"] == pytest.approx(vote_factor(2, 1))


async def test_a_recent_confirmation_adds_a_small_bonus_that_expires(stack, signals):
    review = StaleReview(
        stack.backend.items, stack.backend.kv, stack.backend.audit, stack.backend.clock
    )
    made = (
        await stack.knowledge.remember(ALICE, RememberRequest(title="t", body="b", scope=P1))
    ).item
    assert (await signals.factors([made.id]))[made.id] == 1.0
    await review.confirm(ALICE, made.id)
    assert (await signals.factors([made.id]))[made.id] == pytest.approx(1.0 + CONFIRM_BONUS)
    stack.backend.clock.advance(days=100)
    assert (await signals.factors([made.id]))[made.id] == 1.0


async def test_signals_break_near_ties_but_never_override_clear_relevance(stack, signals):
    consult = ConsultService(
        items=stack.backend.items, text=stack.backend.text, vectors=stack.backend.vectors,
        embedder=stack.embedder, clock=stack.backend.clock, signals=signals,
    )  # fmt: skip
    plain = ConsultService(
        items=stack.backend.items, text=stack.backend.text, vectors=stack.backend.vectors,
        embedder=stack.embedder, clock=stack.backend.clock,
    )  # fmt: skip

    async def add(title, body):
        return (
            await stack.knowledge.remember(ALICE, RememberRequest(title=title, body=body, scope=P1))
        ).item

    best = await add("Radar range equation", "the radar range equation relates power and distance")
    close = await add("Radar range notes", "radar range equation power distance antenna gain notes")
    far = await add("Pelican habits", "pelicans nest on cliffs and eat fish; radar is unrelated")

    async def order(svc):
        found = await svc.consult(BOB, ConsultRequest("radar range equation", limit=5))
        return [m.item_id for m in found.primary]

    base = await order(plain)
    assert base[0] == best.id
    for person in "abcdefgh":  # a crowd finds the second note more useful
        await signals.record_vote("org1", person, close.id, True)
        await signals.record_vote("org1", person, best.id, False)
    nudged = await order(consult)
    assert set(nudged) == set(base)
    assert nudged.index(far.id) == base.index(far.id) or nudged.index(far.id) == len(nudged) - 1
    assert nudged.index(close.id) <= base.index(close.id)  # it can only have moved up
