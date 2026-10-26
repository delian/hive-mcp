"""The hooks against a real server: what they inject, what they store, and that they fail open."""

import json

import pytest
from typer.testing import CliRunner

from hive_mcp.cli import cli
from hive_mcp.client import hooks
from hive_mcp.client.transcript import mechanical_summary, read_turns, render_log
from tests.conformance.factory import ALICE
from tests.integration.conftest import Live

runner = CliRunner()


@pytest.fixture
def connected(live: Live, monkeypatch):
    monkeypatch.setenv("HIVE_URL", f"{live.url}/mcp")
    monkeypatch.setenv("HIVE_TOKEN", live.token_for(ALICE))
    return live


def write_transcript(path, turns, extra=()):
    lines = []
    for role, content in turns:
        lines.append({"type": role, "message": {"role": role, "content": content}})
    lines += list(extra)
    path.write_text("\n".join(json.dumps(x) for x in lines))


async def remember(live: Live, title: str, body: str) -> str:
    async with live.client(live.token_for(ALICE)) as client:
        result = await client.call_tool("kb_remember", {"title": title, "body": body})
    return result.structured_content["id"]


# ---------------------------------------------------------------- injecting knowledge
async def test_session_start_announces_the_knowledge_base_and_lists_what_it_holds(connected):
    item = await remember(connected, "Calibration of the radar", "Calibrate the radar monthly.")
    out = json.loads(await hooks.run("session-start", json.dumps({"cwd": "/work/radar"})))
    context = out["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert "kb_consult" in context["additionalContext"] and item in context["additionalContext"]
    assert "Calibration of the radar" in context["additionalContext"]


async def test_session_start_still_greets_when_nothing_is_known_yet(connected):
    out = json.loads(await hooks.run("session-start", json.dumps({"cwd": "/work/nothing-here"})))
    assert "kb_remember" in out["hookSpecificOutput"]["additionalContext"]


async def test_the_prompt_hook_searches_substantial_requests_only(connected):
    item = await remember(
        connected, "Pulse repetition interval", "The interval limits unambiguous range."
    )
    found = await hooks.run(
        "prompt",
        json.dumps({"prompt": "How does the pulse repetition interval limit unambiguous range?"}),
    )
    assert item in json.loads(found)["hookSpecificOutput"]["additionalContext"]
    short = await hooks.run("prompt", json.dumps({"prompt": "pulse repetition interval"}))
    assert short is None  # three words say too little, although they match an item exactly
    slash = {"prompt": "/explain the pulse repetition interval and unambiguous range"}
    assert await hooks.run("prompt", json.dumps(slash)) is None  # commands are not questions
    unrelated = "Please write a poem about autumn leaves falling in the quiet forest"
    assert (
        await hooks.run("prompt", json.dumps({"prompt": unrelated})) is None
    )  # nothing relevant: silence


# ---------------------------------------------------------------- reporting
async def test_session_end_stores_a_private_report_once(connected, tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(
        transcript,
        [
            ("user", "Tune the dwell time for the scan"),
            ("assistant", [{"type": "text", "text": "Set dwell to 12 ms."}]),
        ],
    )
    payload = json.dumps(
        {
            "transcript_path": str(transcript),
            "cwd": "/work/radar",
            "session_id": "s-1",
            "reason": "other",
        }
    )
    assert await hooks.run("session-end", payload) is None
    assert (
        await hooks.run("session-end", payload) is None
    )  # same session, same content: no duplicate
    async with connected.client(connected.token_for(ALICE)) as client:
        listed = (await client.call_tool("kb_list", {"kinds": ["session"]})).structured_content
        (row,) = listed["items"]  # exactly one report, although the hook ran twice
        stored = (await client.call_tool("kb_get", {"id": row["id"]})).structured_content
    assert row["kind"] == "session" and row["scope"].startswith("private")
    assert "Tune the dwell time" in stored["body"] and "Session in radar" in stored["body"]


async def test_pre_compact_reports_a_compaction(connected, tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(
        transcript, [("user", "Plan the migration"), ("assistant", "Step one is the backup.")]
    )
    await hooks.run(
        "pre-compact",
        json.dumps(
            {
                "transcript_path": str(transcript),
                "cwd": "/w/x",
                "session_id": "s-2",
                "trigger": "auto",
            }
        ),
    )
    async with connected.client(connected.token_for(ALICE)) as client:
        listed = (await client.call_tool("kb_list", {"kinds": ["compaction"]})).structured_content
        (row,) = listed["items"]
        stored = (await client.call_tool("kb_get", {"id": row["id"]})).structured_content
    assert "Plan the migration" in stored["body"] and "(ended: auto)" in stored["body"]


# ---------------------------------------------------------------- failing open
async def test_nothing_ever_raises_and_nothing_is_printed_when_the_server_is_unreachable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HIVE_URL", "http://127.0.0.1:9/mcp")  # nothing listens here
    monkeypatch.setenv("HIVE_TOKEN", "hive_x")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [("user", "hello there")])
    for event, payload in [
        ("session-start", {"cwd": "/w"}),
        ("prompt", {"prompt": "a fairly long question about many things here"}),
        ("pre-compact", {"transcript_path": str(transcript)}),
        ("session-end", {"transcript_path": str(transcript)}),
    ]:
        assert await hooks.run(event, json.dumps(payload)) is None


async def test_unconfigured_malformed_and_unknown_inputs_are_ignored(monkeypatch):
    monkeypatch.delenv("HIVE_URL", raising=False)
    monkeypatch.delenv("HIVE_TOKEN", raising=False)
    monkeypatch.setattr(
        "hive_mcp.client.connection.TOKEN_FILE", __import__("pathlib").Path("/nonexistent/token")
    )
    assert await hooks.run("session-start", "{}") is None
    assert await hooks.run("session-start", "not json") is None
    assert await hooks.run("session-start", "[1,2]") is None
    assert await hooks.run("no-such-event", "{}") is None


def test_the_cli_entry_point_prints_the_hook_output_and_exits_zero(connected):
    out = runner.invoke(cli, ["hook", "session-start"], input=json.dumps({"cwd": "/w/p"}))
    assert out.exit_code == 0 and "hookSpecificOutput" in out.output
    assert runner.invoke(cli, ["hook", "bogus"], input="{}").exit_code == 0


def test_status_reports_the_connection(connected, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "reachable, token accepted" in runner.invoke(cli, ["hooks", "status"]).output
    monkeypatch.setenv("HIVE_TOKEN", "hive_wrong")
    assert "NOT reachable" in runner.invoke(cli, ["hooks", "status"]).output


# ---------------------------------------------------------------- transcripts
def test_transcripts_yield_conversation_text_only(tmp_path):
    path = tmp_path / "t.jsonl"
    write_transcript(
        path,
        [
            ("user", "Question one"),
            (
                "assistant",
                [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "Answer one"}],
            ),
            ("user", "<system-reminder>ignore me</system-reminder>"),
            ("user", [{"type": "tool_result", "content": "big output"}]),
        ],
        extra=[
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"role": "assistant", "content": "subagent chatter"},
            },
            {"type": "summary", "summary": "x"},
        ],
    )
    path.write_text(path.read_text() + "\nnot json at all\n")
    turns = read_turns(path)
    assert [(t.role, t.text) for t in turns] == [
        ("user", "Question one"),
        ("assistant", "Answer one"),
    ]
    assert read_turns(tmp_path / "missing.jsonl") == []


def test_long_logs_keep_the_end_and_summaries_stay_short(tmp_path):
    path = tmp_path / "t.jsonl"
    write_transcript(
        path,
        [
            ("user", "first request"),
            ("assistant", "x" * 5000),
            ("user", "y" * 70000 + " LAST-WORD"),
        ],
    )
    turns = read_turns(path)
    log = render_log(turns)
    assert len(log) < 61_000 and log.startswith("[...earlier") and log.endswith("LAST-WORD")
    assert "first request" not in log  # the oldest turns are what gets dropped
    summary = mechanical_summary(turns, "radar", "other")
    assert (
        summary.startswith("Session in radar: 2 requests, 1 replies (ended: other).")
        and len(summary) <= 1500
    )
    assert "first request" in summary
