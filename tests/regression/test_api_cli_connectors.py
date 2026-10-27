"""Regression tests for the MCP API, connectors, operator CLIs and the hooks client."""

from __future__ import annotations

from hive_mcp.domain.item import Status
from tests.connectors import test_sync as connector_tests
from tests.integration import conftest as integration

Fake, SPEC, titles = connector_tests.Fake, connector_tests.SPEC, connector_tests.titles
sync = connector_tests.sync  # fixtures, reused
live = integration.live


async def test_api_1_document_reverted_to_earlier_content_is_active_again(stack, sync):
    """BUG-API-1: a source document edited and then reverted to its earlier text stays archived."""
    src = Fake()
    src.docs = {"a": ("original words here", "v1")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("edited words here", "v2")}
    await sync.run(SPEC, src)
    src.docs = {"a": ("original words here", "v3")}
    await sync.run(SPEC, src)
    assert await titles(stack) == ["a"]
    assert await titles(stack, Status.ARCHIVED) == ["a"]


async def test_api_1_document_that_vanished_and_returned_is_active_again(stack, sync):
    """BUG-API-1: a retired document that comes back unchanged must be searchable again."""
    src = Fake()
    src.docs = {"a": ("original words here", "v1")}
    await sync.run(SPEC, src)
    src.docs = {}
    await sync.run(SPEC, src)
    assert await titles(stack) == []
    src.docs = {"a": ("original words here", "v1")}
    await sync.run(SPEC, src)
    assert await titles(stack) == ["a"]


async def test_api_2_github_paths_with_reserved_characters_are_encoded():
    """BUG-API-2: `docs/C#.md` was requested as `docs/C` (the rest became a fragment)."""
    import httpx

    from hive_mcp.connectors.github import GithubConnector

    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if "/git/trees/" in request.url.path:
            names = ["C#.md", "what?.md", "100%.md", "a b.md"]
            tree = [{"type": "blob", "path": f"docs/{n}", "size": 3, "sha": "s"} for n in names]
            return httpx.Response(200, json={"tree": tree})
        return httpx.Response(200, content=b"hi")

    connector = GithubConnector(
        "o/r", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    docs = [d async for d in connector.documents()]
    for doc in docs:
        await doc.load()
    raw_paths = sorted(u.raw_path.decode().split("?")[0] for u in seen[1:])
    assert raw_paths == sorted(
        f"/repos/o/r/contents/docs/{n}" for n in ("C%23.md", "what%3F.md", "100%25.md", "a%20b.md")
    )
    assert all(u.params["ref"] == "HEAD" and not u.fragment for u in seen[1:])


async def test_api_3_sharepoint_follows_next_links_with_their_query():
    """BUG-API-3: `params={}` stripped the query of @odata.nextLink: page 1 was fetched forever."""
    import httpx

    from hive_mcp.connectors.sharepoint import SharePointConnector

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        page = request.url.params.get("page", "1")
        entry = {"id": f"id{page}", "name": f"f{page}.md", "file": {}, "size": 1, "eTag": "e"}
        body: dict = {"value": [entry]}
        if page == "1":
            link = "https://graph.microsoft.com/v1.0/drives/d/root/children?$top=200&page=2"
            body["@odata.nextLink"] = link
        return httpx.Response(200, json=body)

    connector = SharePointConnector(
        "t", "c", "s", "d", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert [d.key async for d in connector.documents()] == ["id1", "id2"]


def test_api_4_connector_settings_of_the_wrong_type_are_validation_errors():
    """BUG-API-4: tags as a string, `every_minutes` as text/null/fraction crashed or were misread."""
    import pytest

    from hive_mcp.connectors.spec import parse_specs
    from hive_mcp.domain.errors import ValidationError

    base = {"name": "w", "type": "urls", "urls": ["https://x"], "org": "o", "owner": "b"}
    base["project"] = "p"
    for bad in (
        {"every_minutes": "abc"}, {"every_minutes": None}, {"every_minutes": 7.5},
        {"tags": None}, {"tags": "source/wiki"}, {"tags": [1]},
    ):  # fmt: skip
        with pytest.raises(ValidationError):
            parse_specs([{**base, **bad}])
    ok = parse_specs([{**base, "every_minutes": 10, "tags": ["source/wiki"]}])[0]
    assert ok.every_minutes == 10 and ok.tags == ("source/wiki",)


def _transcript(tmp_path, *entries):
    import json

    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


def test_api_5_transcript_turns_with_unicode_line_separators_are_kept(tmp_path):
    """BUG-API-5: splitlines() cut a JSON line at U+2028/U+0085 inside a message: the turn was lost."""
    from hive_hooks.transcript import read_turns

    path = _transcript(
        tmp_path,
        {"message": {"role": "user", "content": "first second\u0085third"}},
        {"message": {"role": "assistant", "content": "answer"}},
    )
    turns = read_turns(path)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].text == "first second\u0085third"


def test_api_6_heredoc_commit_messages_are_read_properly(tmp_path):
    """BUG-API-6: `git commit -m "$(cat <<'EOF' ...` was reported as the commit "$(cat <<'EOF'"."""
    from hive_hooks.transcript import read_activity

    heredoc = (
        "git add -A && git commit -m \"$(cat <<'EOF'\nFix the cache keys\n\nLonger body\nEOF\n)\""
    )
    path = _transcript(
        tmp_path,
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": heredoc}},
            {"type": "tool_use", "name": "Bash", "input": {"command": 'git commit -m "plain one"'}},
        ]}},
    )  # fmt: skip
    assert read_activity(path).commits == ("Fix the cache keys", "plain one")


def test_api_7_install_keeps_lookalike_and_shared_entry_hooks():
    """BUG-API-7: a hook whose path contains `hive`, or one sharing an entry with ours, was deleted."""
    from hive_hooks import install

    lookalike = {"hooks": [{"type": "command", "command": "/home/hive/bin/lint hook check"}]}
    shared = {
        "matcher": "",
        "hooks": [
            {"type": "command", "command": "./audit.sh"},
            {"type": "command", "command": "hive hook session-start"},
        ],
    }
    settings = {"hooks": {"SessionStart": [lookalike, shared], "Stop": [lookalike]}}
    merged = install.merge(settings)
    start = merged["hooks"]["SessionStart"]
    assert lookalike in start
    assert {"matcher": "", "hooks": [{"type": "command", "command": "./audit.sh"}]} in start
    assert merged["hooks"]["Stop"] == [lookalike]
    removed = install.remove(merged)
    assert removed["hooks"]["Stop"] == [lookalike] and lookalike in removed["hooks"]["SessionStart"]
    assert install.installed_events(settings) == ["SessionStart"]  # the shared entry is ours too
    assert install.installed_events({"hooks": {"Stop": [lookalike]}}) == []


def test_api_8_saving_settings_keeps_symlinks_and_permissions(tmp_path):
    """BUG-API-8: install replaced a symlinked settings.json with a regular 0600 file."""
    import os

    from hive_hooks import install

    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    real.chmod(0o644)
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(real)
    install.save(link, {"a": 1})
    assert link.is_symlink() and '"a": 1' in real.read_text()
    assert os.stat(real).st_mode & 0o777 == 0o644


async def test_api_9_a_slow_summarizer_leaves_the_upload_less_time(tmp_path, monkeypatch):
    """BUG-API-9: summarizer (up to 25 s) plus upload (15 s) outlived the 30 s hook limit, losing the report."""
    from hive_hooks import hooks

    path = _transcript(
        tmp_path, {"message": {"role": "user", "content": "please fix the parser"}},
        {"message": {"role": "assistant", "content": "We fixed the parser because tokens were dropped."}},
    )  # fmt: skip
    clock = [1000.0]  # the summarizer below takes 25 s: the most it may take
    monkeypatch.setattr(hooks.time, "monotonic", lambda: clock[0])

    async def slow_summary(*_a, **_k):
        clock[0] += 25.0
        return "summary"

    deadlines = []

    async def fake_call(name, args, deadline=5.0):
        deadlines.append(deadline)
        return {}

    monkeypatch.setattr(hooks, "summarize", slow_summary)
    monkeypatch.setattr(hooks, "call_tool", fake_call)
    await hooks.session_end({"transcript_path": str(path), "session_id": "abcd1234"})
    assert 25.0 + deadlines[0] < 30.0 and deadlines[0] >= 3.0


async def test_api_10_a_bad_session_label_is_refused_before_the_report_is_stored(live):
    """BUG-API-10: kb_report_session stored the note, then failed on the label: a retry duplicated it."""
    from tests.conformance.factory import ALICE

    async with live.client(live.token_for(ALICE)) as client:
        bad = await client.call_tool("kb_report_session", {"summary": "did things", "session": "a"})
        assert bad.is_error and "session labels" in bad.content[0].text
        listed = await client.call_tool("kb_list", {"kinds": ["session"]})
        assert listed.structured_content["items"] == []
        ok = await client.call_tool(
            "kb_report_session", {"summary": "did things", "session": "cc-abc123"}
        )
        assert not ok.is_error
        for tool, args in (
            ("kb_consult", {"query": "x"}),
            ("kb_get", {"id": ok.structured_content["id"]}),
        ):
            refused = await client.call_tool(tool, {**args, "session": "!!"})
            assert refused.is_error and "session labels" in refused.content[0].text


async def test_api_11_the_url_fetching_tool_is_marked_open_world(live):
    """BUG-API-11: kb_ingest_url fetches from the web but claimed `openWorldHint: false`."""
    from tests.conformance.factory import ALICE

    async with live.client(live.token_for(ALICE)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["kb_ingest_url"].annotations.open_world_hint is True
    assert tools["kb_ingest_url"].annotations.read_only_hint is False
    assert all(
        t.annotations.open_world_hint is False for n, t in tools.items() if n != "kb_ingest_url"
    )


def test_api_12_ingest_git_does_not_store_credentials_from_the_clone_address(tmp_path, monkeypatch):
    """BUG-API-12: `https://user:TOKEN@host/repo` was stored verbatim as each item's source."""
    import asyncio

    from typer.testing import CliRunner

    from hive_mcp.bootstrap import build_services
    from hive_mcp.cli import cli
    from hive_mcp.cli_ingest import public_url
    from hive_mcp.config import Settings
    from hive_mcp.domain.item import ItemQuery
    from hive_mcp.domain.principal import Principal, Role

    assert public_url("https://u:tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert public_url("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert public_url("git@github.com:o/r.git") == "git@github.com:o/r.git"

    def fake_clone(url, destination, ref=None):
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("# Readme\nradar pulse timing notes")

    monkeypatch.setattr("hive_mcp.ingest.git.clone", fake_clone)
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    url = "https://x-access-token:ghp_SECRET123@github.com/acme/docs.git"
    out = CliRunner().invoke(cli, ["ingest-git", url, "--org", "o1", "--user", "u1"])
    assert out.exit_code == 0, out.output

    async def sources():
        services = await build_services(Settings())
        try:
            who = Principal("u1", "o1", Role.MEMBER, frozenset(), frozenset())
            page = await services.knowledge.browse(who, ItemQuery(limit=10))
            return [v.item.source for v in page.items]
        finally:
            await services.close()

    found = asyncio.run(sources())
    assert found == ["git:https://github.com/acme/docs.git#README.md"]


def test_api_13_jira_text_keeps_line_breaks_mentions_and_links():
    """BUG-API-13: hard breaks glued words together; mentions and smart links vanished."""
    from hive_mcp.connectors.atlassian import adf_text

    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Ask "},
        {"type": "mention", "attrs": {"id": "1", "text": "@Bob"}},
        {"type": "text", "text": " to review"},
        {"type": "hardBreak"},
        {"type": "text", "text": "see "},
        {"type": "inlineCard", "attrs": {"url": "https://x.example/doc"}},
    ]}]}  # fmt: skip
    assert adf_text(doc).strip() == "Ask @Bob to review\nsee https://x.example/doc"


async def test_api_13_notion_tables_and_checked_to_dos_are_flattened():
    """BUG-API-13: table rows (text in `cells`) were dropped and every to-do looked unchecked."""
    import httpx

    from hive_mcp.connectors.notion import NotionConnector

    def rt(text):
        return [{"plain_text": text}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            page = {"object": "page", "id": "p1", "properties": {}, "last_edited_time": "t"}
            return httpx.Response(200, json={"results": [page]})
        blocks = [
            {"id": "b1", "type": "table_row", "table_row": {"cells": [rt("Name"), rt("Value")]}},
            {"id": "b2", "type": "to_do", "to_do": {"rich_text": rt("ship it"), "checked": True}},
            {"id": "b3", "type": "to_do", "to_do": {"rich_text": rt("test it"), "checked": False}},
        ]
        return httpx.Response(200, json={"results": blocks})

    connector = NotionConnector(
        "tok", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    [doc] = [d async for d in connector.documents()]
    _, data = await doc.load()
    text = data.decode()
    assert "Name | Value" in text and "- [x] ship it" in text and "- [ ] test it" in text
