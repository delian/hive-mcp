"""Session summaries: activity from tool calls, key sentences, and the optional external summarizer."""

import json
import stat
import sys
import textwrap
import time

import pytest
from hive_hooks import summary
from hive_hooks.summary import extractive_summary, key_points, outcomes, summarize
from hive_hooks.transcript import Activity, Turn, read_activity


def entry(role, content, **extra):
    return {"type": role, "message": {"role": role, "content": content}, **extra}


def tool(name, **inputs):
    return {"type": "tool_use", "name": name, "input": inputs}


def write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries))


# ---------------------------------------------------------------- what the agent did
def test_files_and_commits_come_from_tool_calls_in_order_without_repeats(tmp_path):
    path = tmp_path / "t.jsonl"
    write(
        path,
        [
            entry("user", "fix the rate limiter"),
            entry(
                "assistant",
                [tool("Edit", file_path="/src/a.py"), tool("Write", file_path="/src/b.py")],
            ),
            entry(
                "assistant",
                [
                    tool("Edit", file_path="/src/a.py"),
                    tool("NotebookEdit", notebook_path="/n.ipynb"),
                ],
            ),
            entry(
                "assistant", [tool("Read", file_path="/src/never.py"), tool("Bash", command="ls")]
            ),
            entry(
                "assistant",
                [tool("Bash", command='git commit -m "Fix burst handling" && git push')],
            ),
            entry(
                "assistant", [tool("Bash", command="git commit -am 'Second: with colon\n\nbody'")]
            ),
            entry(
                "assistant", [tool("Edit", file_path="/side.py")], isSidechain=True
            ),  # a subagent's work
        ],
    )
    activity = read_activity(path)
    assert activity.files == ("/src/a.py", "/src/b.py", "/n.ipynb")
    assert activity.commits == ("Fix burst handling", "Second: with colon")
    assert activity.commands == 3


def test_odd_tool_calls_do_not_break_reading(tmp_path):
    path = tmp_path / "t.jsonl"
    write(
        path,
        [
            entry(
                "assistant",
                [tool("Edit"), {"type": "tool_use", "name": "Bash", "input": "oops"}, "junk", None],
            ),
            entry("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": 5}}]),
            {"type": "summary"},
        ],
    )
    assert read_activity(path) == Activity((), (), 0)
    assert read_activity(tmp_path / "missing") == Activity((), (), 0)


def test_outcomes_list_commits_and_files_with_a_cap():
    many = Activity(tuple(f"/f{i}.py" for i in range(9)), tuple(f"c{i}" for i in range(9)), 3)
    items = outcomes(many)
    assert items[:5] == [f"commit: c{i}" for i in range(5)] and len(items) <= 8
    assert items[-1] == "edited 9 file(s): /f0.py, /f1.py, /f2.py, /f3.py and 5 more"
    assert outcomes(Activity((), (), 7)) == []


# ---------------------------------------------------------------- picking sentences
def conversation():
    return [
        Turn("user", "Why does the rate limiter let bursts through?"),
        Turn("assistant", "Let me look at the code first. Looking at ratelimit.py now."),
        Turn(
            "assistant",
            "The root cause is that the bucket refills before the check, so a client can send 2x the burst.",
        ),
        Turn("assistant", "```python\nfor x in y: pass\n```"),
        Turn(
            "assistant",
            "I fixed it by moving the refill after the check; the new test passes and measured overshoot is 0.",
        ),
        Turn("assistant", "Sure, anything else?"),
    ]


def test_informative_sentences_win_and_filler_and_code_lose():
    points = key_points(conversation(), 700)
    joined = " ".join(points)
    assert "root cause" in joined and "fixed it" in joined
    assert "Let me look" not in joined and "Sure" not in joined and "for x in y" not in joined
    assert points.index(next(p for p in points if "root cause" in p)) < points.index(
        next(p for p in points if "fixed it" in p)
    )  # chronological


def test_the_summary_has_the_request_the_points_and_the_work_and_stays_bounded():
    activity = Activity(("/src/limiter.py",), ("Refill after the check",), 4)
    text = extractive_summary(conversation(), activity, "radar", "other")
    assert text.startswith("Session in radar: 1 request(s) (ended: other).")
    assert "Asked: Why does the rate limiter let bursts through?" in text
    assert "Key points:" in text and "root cause" in text
    assert "Commit: Refill after the check" in text and "Edited 1 file(s): /src/limiter.py" in text
    huge = [Turn("user", "u" * 5000)] + [
        Turn("assistant", f"We fixed bug number {i} because of cause {i}. " * 5) for i in range(40)
    ]
    assert len(extractive_summary(huge, Activity((), (), 0), "p")) <= 1500


def test_sessions_without_prose_still_get_a_line():
    only_user = [Turn("user", "hello there my friend")]
    assert "Asked: hello there my friend" in extractive_summary(only_user, Activity((), (), 0), "p")
    ends_with_chatter = [Turn("user", "do it"), Turn("assistant", "ok")]
    assert "Last reply: ok" in extractive_summary(ends_with_chatter, Activity((), (), 0), "p")
    assert key_points([], 100) == []


# ---------------------------------------------------------------- the user's own summarizer
def script(tmp_path, body, name="summarizer.py"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {path}"


async def test_an_external_summarizer_receives_the_conversation_and_replaces_the_summary(
    tmp_path, monkeypatch
):
    command = script(
        tmp_path,
        """
        import sys
        text = sys.stdin.read()
        print("MODEL SAYS: " + str(text.count("[user]")) + " user turn(s); saw " + str("root cause" in text))
    """,
    )
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER", command)
    text = await summarize(conversation(), Activity((), (), 0), "radar", "other")
    assert (
        text.startswith("Session in radar (ended: other).")
        and "MODEL SAYS: 1 user turn(s); saw True" in text
    )


@pytest.mark.parametrize(
    "body", ["import sys; sys.exit(3)", "print('')", "import time; time.sleep(5)"]
)
async def test_a_failing_empty_or_slow_summarizer_falls_back_to_the_extractive_summary(
    tmp_path, monkeypatch, body
):
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER", script(tmp_path, body))
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER_TIMEOUT", "0.5")
    text = await summarize(conversation(), Activity((), (), 0), "radar")
    assert "Key points:" in text and "root cause" in text


async def test_a_missing_command_or_bad_quoting_falls_back_and_nothing_runs_unconfigured(
    tmp_path, monkeypatch
):
    for bad in ("/no/such/program --x", "unterminated 'quote"):
        monkeypatch.setenv("HIVE_HOOK_SUMMARIZER", bad)
        assert "Key points:" in await summarize(conversation(), Activity((), (), 0), "p")
    monkeypatch.delenv("HIVE_HOOK_SUMMARIZER")
    assert await summary.external_summary(conversation()) is None


async def test_external_output_is_capped_and_the_command_is_never_run_through_a_shell(
    tmp_path, monkeypatch
):
    marker = tmp_path / "pwned"
    command = script(tmp_path, "print('x' * 5000)")
    monkeypatch.setenv(
        "HIVE_HOOK_SUMMARIZER", f"{command} ; touch {marker}"
    )  # `;` is just an argument
    text = await summarize(conversation(), Activity((), (), 0), "p")
    assert len(text) <= 2000 and not marker.exists()


# ---------------------------------------------------------------- finer points
def test_more_informative_sentences_beat_bland_ones_when_space_is_short():
    bland = [
        Turn(
            "assistant",
            f"This is background paragraph number {i} about the general layout of things.",
        )
        for i in range(6)
    ]
    rich = Turn(
        "assistant",
        "We fixed the deadlock because the lock order was reversed, which cut latency from 900 ms to 40 ms.",
    )
    points = key_points([Turn("user", "why slow?"), *bland, rich, *bland], 700)
    assert any("deadlock" in p for p in points) and len(points) <= 5


def test_code_like_text_is_not_a_key_point():
    code = "for item in items: result = fix(item); other = {1: (2, 3)}; return [result, other]"
    turns = [
        Turn("user", "go"),
        Turn("assistant", code + ". We fixed the parser because tokens were dropped."),
    ]
    assert all("for item in items" not in p for p in key_points(turns, 700))


def test_the_last_reply_and_later_work_count_more_and_the_score_never_goes_negative():
    s = "We fixed the cache because keys collided across tenants."
    assert (
        summary._score(s, 4, 5, True)
        > summary._score(s, 4, 5, False)
        > summary._score(s, 0, 5, False)
        > 0
    )
    assert (
        summary._score("Nothing much of interest happens in this sentence at all.", 0, 5, False) > 0
    )


async def test_a_summarizer_that_is_too_slow_is_killed_not_left_running(tmp_path, monkeypatch):
    import os  # noqa: PLC0415

    pid_file = tmp_path / "pid"
    command = script(
        tmp_path,
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)",
    )
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER", command)
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER_TIMEOUT", "1")
    started = time.monotonic()
    assert await summary.external_summary(conversation()) is None
    assert time.monotonic() - started < 10  # it was killed, not waited for
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # gone


async def test_output_from_a_summarizer_that_failed_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "HIVE_HOOK_SUMMARIZER", script(tmp_path, "import sys; print('half an answer'); sys.exit(1)")
    )
    assert await summary.external_summary(conversation()) is None


async def test_the_external_text_itself_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOOK_SUMMARIZER", script(tmp_path, "print('x' * 9000)"))
    assert len(await summary.external_summary(conversation())) == 2000
