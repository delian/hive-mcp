"""kb_digest over HTTP, the preview command, and the weekly loop's timing."""

import asyncio
import json

from typer.testing import CliRunner

from hive_mcp.cli import cli
from hive_mcp.ops.loops import digest_loop
from hive_mcp.security.directory import DirectoryUser
from tests.conformance.factory import ALICE, BOB
from tests.integration.conftest import Live

runner = CliRunner()


async def test_the_tool_reports_what_others_added(live: Live):
    async with live.client(live.token_for(BOB)) as bob:
        await bob.call_tool(
            "kb_remember",
            {
                "title": "Dwell time plan",
                "body": "Use 12 ms.",
                "share_with_project": "p1",
                "tags": ["domain/rf"],
            },
        )
        await bob.call_tool("kb_remember", {"title": "Bob's private idea", "body": "secret plans"})
    async with live.client(live.token_for(ALICE)) as alice:
        out = (await alice.call_tool("kb_digest", {})).structured_content
        bad = await alice.call_tool("kb_digest", {"days": 500})
    assert out["new_items"] == 1 and [i["title"] for i in out["items"]] == ["Dwell time plan"]
    assert out["items"][0]["origin"] == "project p1" and out["top_tags"] == {"domain/rf": 1}
    assert "Dwell time plan" in out["text"] and "secret" not in json.dumps(out)
    assert bad.is_error and "days must be" in bad.content[0].text
    async with live.client(live.token_for(BOB)) as bob:
        assert (await bob.call_tool("kb_digest", {})).structured_content[
            "new_items"
        ] == 0  # his own


def test_the_preview_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    assert "give --user" in runner.invoke(cli, ["digest"]).output
    assert "no active user" in runner.invoke(cli, ["digest", "--user", "ghost@acme.com"]).output
    from hive_mcp.config import Settings  # noqa: PLC0415
    from hive_mcp.domain.principal import Role  # noqa: PLC0415
    from hive_mcp.security.directory import UserDirectory  # noqa: PLC0415
    from hive_mcp.security.jsonfile import AtomicJsonFile  # noqa: PLC0415

    UserDirectory(AtomicJsonFile(Settings().users_path, default=[])).upsert(
        DirectoryUser("ann@acme.com", "acme", Role.MEMBER)
    )
    out = runner.invoke(cli, ["digest", "--user", "ann@acme.com"])
    assert out.exit_code == 0 and "What is new in the team knowledge base" in out.output
    sending = runner.invoke(cli, ["digest", "--send"])
    assert sending.exit_code == 1 and "HIVE_SMTP_HOST" in sending.output


class FakeSender:
    def __init__(self, failed=0):
        self.calls, self.failed = 0, failed

    async def send_all(self):
        self.calls += 1
        return (1, self.failed)


class FakeServices:
    def __init__(self, sender):
        self.digest_sender = sender


async def run_loop(sender, monkeypatch, now, rounds):
    class Clock:
        def now(self):
            return now

    monkeypatch.setattr("hive_mcp.ops.loops.SystemClock", Clock)
    monkeypatch.setattr("hive_mcp.ops.loops.DIGEST_CHECK_SECONDS", 0.01)
    task = asyncio.create_task(digest_loop(FakeServices(sender), weekday=0, hour=8))
    await asyncio.sleep(0.01 * rounds)
    task.cancel()


async def test_the_loop_sends_in_the_chosen_hour_once_per_week(monkeypatch):
    from datetime import UTC, datetime  # noqa: PLC0415

    monday_9 = datetime(2026, 1, 5, 9, tzinfo=UTC)  # a Monday
    sender = FakeSender()
    await run_loop(sender, monkeypatch, monday_9, 8)
    assert sender.calls == 1  # many checks, one send
    before, tuesday = FakeSender(), datetime(2026, 1, 6, 9, tzinfo=UTC)
    await run_loop(before, monkeypatch, tuesday, 5)
    early = FakeSender()
    await run_loop(early, monkeypatch, datetime(2026, 1, 5, 7, tzinfo=UTC), 5)
    assert before.calls == 0 and early.calls == 0


async def test_failures_are_retried_at_the_next_check(monkeypatch):
    from datetime import UTC, datetime  # noqa: PLC0415

    sender = FakeSender(failed=1)
    await run_loop(sender, monkeypatch, datetime(2026, 1, 5, 9, tzinfo=UTC), 6)
    assert sender.calls >= 3


async def test_the_tool_reports_edits_and_kb_list_can_sort_by_last_change(live: Live):
    async with live.client(live.token_for(BOB)) as bob:
        old = (
            await bob.call_tool(
                "kb_remember", {"title": "Older note", "body": "v1", "share_with_project": "p1"}
            )
        ).structured_content
        newer = (
            await bob.call_tool(
                "kb_remember", {"title": "Newer note", "body": "v1", "share_with_project": "p1"}
            )
        ).structured_content
        await bob.call_tool(
            "kb_update", {"id": old["id"], "expected_revision": 1, "title": "Older note, edited"}
        )
        by_created = (await bob.call_tool("kb_list", {})).structured_content["items"]
        by_updated = (await bob.call_tool("kb_list", {"sort": "updated"})).structured_content[
            "items"
        ]
    assert [i["id"] for i in by_created][:2] == [newer["id"], old["id"]]
    assert [i["id"] for i in by_updated][:2] == [old["id"], newer["id"]]  # the edited one rises
    async with live.client(live.token_for(ALICE)) as alice:
        out = (await alice.call_tool("kb_digest", {})).structured_content
    assert (
        out["edited"] == [] and out["new_items"] == 2
    )  # both are new this week; nothing is an "old item edited"
