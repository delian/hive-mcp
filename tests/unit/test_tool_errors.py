import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hive_mcp.domain.errors import (
    Conflict,
    HiveError,
    NotFound,
    PermissionDenied,
    StorageBusy,
    ValidationError,
)
from hive_mcp.mcp_api.errors import guarded, user_message


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NotFound("item-123-in-other-org"), "not found, or not visible to you"),
        (Conflict("revision 3 != expected 1"), "kb_get"),
        (StorageBusy("pool exhausted: host db-7"), "temporarily unavailable"),
        (PermissionDenied("cannot create in this scope"), "cannot create in this scope"),
        (ValidationError("title must be 1-200 characters"), "title must be"),
    ],
)
def test_user_messages_are_actionable_and_do_not_leak_details(exc, expected):
    message = user_message(exc)
    assert expected in message
    assert "item-123" not in message and "db-7" not in message and "pool exhausted" not in message


async def test_guarded_converts_expected_errors_and_masks_unexpected_ones():
    @guarded
    async def hive():
        raise Conflict("stale")

    @guarded
    async def bug():
        raise RuntimeError("secret path /etc/hive/key and password hunter2")

    @guarded
    async def fine():
        return 7

    with pytest.raises(ToolError, match="kb_get"):
        await hive()
    with pytest.raises(ToolError) as caught:
        await bug()
    assert "hunter2" not in str(caught.value) and "/etc" not in str(caught.value)
    assert "internal error" in str(caught.value) and caught.value.__cause__ is None
    assert await fine() == 7
    assert isinstance(Conflict("x"), HiveError)
