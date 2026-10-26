from datetime import UTC, datetime

from hive_mcp.domain.access import Tier
from hive_mcp.domain.item import Item, ItemView
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.mcp_api.resources import render_item
from hive_mcp.mcp_api.tools import BODY_LIMIT

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def view(tier, body="text", **kw):
    item = Item(
        "i1", "org", "note", "Title", body, Scope(ScopeLevel.PROJECT, "p9"), "carol", NOW, NOW,
        Visibility.ORG, **kw,
    )  # fmt: skip
    return ItemView(item, tier)


def test_other_groups_items_are_labelled_external():
    text = render_item(view(Tier.REFERENCED))
    assert "EXTERNAL" in text and "carol" in text and text.startswith("# Title")
    assert "EXTERNAL" not in render_item(view(Tier.PRIMARY))


def test_long_bodies_are_truncated_and_say_so():
    text = render_item(view(Tier.PRIMARY, body="x" * (BODY_LIMIT + 5)))
    assert text.endswith("[truncated]") and "x" * BODY_LIMIT in text
    assert "(none)" in text  # no tags
