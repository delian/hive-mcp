"""Fork, hide and suggest over real HTTP: who sees what, and the review round trip."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.conformance.factory import ALICE, CAROL, ROOT
from tests.integration.test_mcp_e2e import call

if TYPE_CHECKING:
    from tests.integration.conftest import Live


async def shared_by_carol(carol, root) -> dict:
    item = (
        await call(carol, "kb_remember", title="Radar tuning", body="use a 3 dB margin")
    ).structured_content
    asked = await call(
        carol,
        "kb_request_promotion",
        id=item["id"],
        to="project",
        owner="p2",
        org_wide=True,
        reason="useful to everyone",
    )
    assert not asked.is_error, asked.content
    req = asked.structured_content
    done = await call(root, "kb_decide_promotion", id=req["id"], decision="approve")
    assert not done.is_error, done
    return item


async def test_fork_hide_and_suggest_over_mcp(live: Live) -> None:
    async with (
        live.client(live.token_for(ALICE)) as alice,
        live.client(live.token_for(CAROL)) as carol,
        live.client(live.token_for(ROOT)) as root,
    ):
        original = await shared_by_carol(carol, root)
        revision = (await call(root, "kb_get", id=original["id"])).structured_content["revision"]
        seen = (await call(alice, "kb_consult", query="radar tuning margin")).structured_content
        assert [r["id"] for r in seen["from_other_groups"]] == [original["id"]]
        assert seen["from_other_groups"][0]["external"] is True

        fork = (await call(alice, "kb_fork", id=original["id"])).structured_content
        assert fork["created"] is True and fork["scope"].startswith("private")
        diff = (await call(alice, "kb_diff_upstream", id=fork["id"])).structured_content
        assert diff["identical"] is True and diff["upstream_revision"] == revision

        hidden = (await call(alice, "kb_ignore", id=original["id"])).structured_content
        assert hidden == {"id": original["id"], "hidden": True}
        after = (await call(alice, "kb_consult", query="radar tuning margin")).structured_content
        assert after["from_other_groups"] == []  # hidden from her results only
        assert fork["id"] in [r["id"] for r in after["results"]]  # her fork still shows
        carols = (await call(carol, "kb_consult", query="radar tuning margin")).structured_content
        assert [r["id"] for r in carols["results"]] == [original["id"]]
        shown = (await call(alice, "kb_ignore", id=original["id"], hide=False)).structured_content
        assert shown["hidden"] is False
        assert (await call(alice, "kb_ignore", id=fork["id"])).is_error  # her own note

        sug = (
            await call(
                alice,
                "kb_suggest_edit",
                id=original["id"],
                new_body="use a 6 dB margin",
                note="measured",
            )
        ).structured_content
        assert sug["status"] == "open" and sug["item_revision"] == revision
        assert (await call(alice, "kb_list_suggestions")).structured_content["result"] == []
        queue = (await call(root, "kb_list_suggestions")).structured_content["result"]
        assert [q["id"] for q in queue] == [sug["id"]]
        assert (await call(alice, "kb_decide_suggestion", id=sug["id"], decision="accept")).is_error
        done = await call(root, "kb_decide_suggestion", id=sug["id"], decision="accept")
        assert done.structured_content["status"] == "accepted"
        now = (await call(root, "kb_get", id=original["id"])).structured_content
        assert now["body"] == "use a 6 dB margin" and now["revision"] == revision + 1
