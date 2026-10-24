import pytest

from hive_mcp.domain.errors import NotFound, ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.services.models import ConsultRequest
from hive_mcp.services.sessions import MAX_LOG_BYTES, SessionReport
from tests.conformance.factory import ALICE, BOB


async def test_session_report_is_private_searchable_and_tagged(stack):
    report = SessionReport(
        summary="Fixed the timing bug in the pulse counter\nLonger explanation here.",
        outcomes=("counter overflow handled", "tests added"),
        project="radar-x",
        tags=("hardware",),
    )
    saved = await stack.sessions.report_session(ALICE, report)
    item = saved.item
    assert (
        item.kind is Kind.SESSION
        and item.scope.level is ScopeLevel.PRIVATE
        and item.author_id == "alice"
    )
    assert item.title.startswith("Session: Fixed the timing bug")
    assert {"hardware", "kind/session", "project/radar-x"} <= item.tags
    assert "- counter overflow handled" in item.body
    found = await stack.consult.consult(ALICE, ConsultRequest("pulse counter overflow"))
    assert [m.item_id for m in found.primary] == [item.id]
    assert (await stack.consult.consult(BOB, ConsultRequest("pulse counter"))).primary == ()


async def test_compaction_report_kind(stack):
    saved = await stack.sessions.report_compaction(
        ALICE, SessionReport(summary="Condensed context of the sonar work")
    )
    assert saved.item.kind is Kind.COMPACTION and "kind/compaction" in saved.item.tags


async def test_log_is_redacted_stored_as_a_file_and_access_controlled(stack):
    secret = "xoxb-1234567890-abcdefghij"  # noqa: S105 - fake test token
    log = f"step 1 ok\nexport SLACK_TOKEN={secret}\nstep 2 ok\n" + "Радар 雷达 " * 50
    saved = await stack.sessions.report_session(ALICE, SessionReport(summary="Deploy run", log=log))
    assert saved.redactions.get("slack-token") == 1 and saved.item.source.startswith("blob:")
    text = await stack.sessions.get_log(ALICE, saved.item.id)
    assert secret not in text and "[REDACTED:slack-token]" in text and "Радар 雷达" in text
    with pytest.raises(NotFound):
        await stack.sessions.get_log(BOB, saved.item.id)  # private to alice
    assert secret not in saved.item.body


async def test_report_without_log_has_no_log_and_oversized_log_is_rejected(stack):
    saved = await stack.sessions.report_session(ALICE, SessionReport(summary="No log here"))
    with pytest.raises(NotFound):
        await stack.sessions.get_log(ALICE, saved.item.id)
    with pytest.raises(ValidationError):
        await stack.sessions.report_session(
            ALICE, SessionReport(summary="Huge", log="x" * (MAX_LOG_BYTES + 1))
        )


async def test_session_report_is_idempotent(stack):
    report = SessionReport(summary="Same report", log="log line", idempotency_key="sess-1")
    first = await stack.sessions.report_session(ALICE, report)
    again = await stack.sessions.report_session(ALICE, report)
    assert first.created and not again.created and first.item.id == again.item.id
