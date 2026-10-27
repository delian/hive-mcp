"""Confluence, Jira, Notion and SharePoint connectors against mocked HTTP."""

import base64
import json

import httpx
import pytest

from hive_mcp.config import Settings
from hive_mcp.connectors.atlassian import ConfluenceConnector, JiraConnector, adf_text
from hive_mcp.connectors.factory import make_connector
from hive_mcp.connectors.notion import NotionConnector, page_title, rich_text
from hive_mcp.connectors.sharepoint import SharePointConnector
from hive_mcp.connectors.spec import parse_specs
from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.oauth.upstream import UpstreamError


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def collect(connector):
    return [d async for d in connector.documents()]


# ---------------------------------------------------------------- Confluence
async def test_confluence_pages_are_listed_with_versions_and_loaded_on_demand():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/rest/api/content"):
            start = int(request.url.params["start"])
            rows = [
                {"id": str(i), "title": f"Page {i}", "version": {"number": i + 1}}
                for i in range(start, min(start + 50, 60))
            ]
            return httpx.Response(200, json={"results": rows})
        return httpx.Response(200, json={"body": {"storage": {"value": "<p>hello &amp; bye</p>"}}})

    docs = await collect(
        ConfluenceConnector(
            "https://acme.atlassian.net/wiki/", "ENG", "a@b.c", "tok", http=client(handler)
        )
    )
    assert (
        len(docs) == 60
        and docs[0].key == "0"
        and docs[0].version == "1"
        and docs[0].tags == ("space/eng",)
    )
    assert [r.url.params["start"] for r in seen] == ["0", "50"]  # paged
    name, data = await docs[3].load()
    assert name == "Page 3.html" and data == b"<h1>Page 3</h1><p>hello &amp; bye</p>"


def test_confluence_and_jira_refuse_bad_input_and_use_basic_auth():
    for base in ("http://acme.atlassian.net", "ftp://x", ""):
        with pytest.raises(ValidationError, match="https"):
            ConfluenceConnector(base, "ENG", "a@b.c", "t")
    for space in ("", "a b", "x/y"):
        with pytest.raises(ValidationError):
            ConfluenceConnector("https://x.atlassian.net", space, "a@b.c", "t")
    with pytest.raises(ValidationError):
        JiraConnector("https://x.atlassian.net", "  ", "a@b.c", "t")
    header = ConfluenceConnector("https://x.atlassian.net", "ENG", "a@b.c", "tok")._http.headers[
        "authorization"
    ]
    assert base64.b64decode(header.removeprefix("Basic ")) == b"a@b.c:tok"


# ---------------------------------------------------------------- Jira
DOC = {"type": "doc", "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "Deploys fail on "}, {"type": "text", "text": "fridays"}]},
    {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "check logs"}]}]}]},
]}  # fmt: skip


def test_the_atlassian_document_format_is_flattened_to_text():
    text = adf_text(DOC)
    assert "Deploys fail on fridays" in text and "check logs" in text
    assert adf_text(None) == "" and adf_text("plain") == "plain" and adf_text(5) == ""


async def test_jira_issues_follow_the_page_token_and_render_comments():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/jql"):
            if request.url.params.get("nextPageToken") == "t2":
                return httpx.Response(
                    200,
                    json={
                        "issues": [{"key": "ENG-2", "fields": {"updated": "u2"}}],
                        "isLast": True,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "issues": [{"key": "ENG-1", "fields": {"updated": "u1"}}],
                    "nextPageToken": "t2",
                },
            )
        fields = {"summary": "Deploys fail", "description": DOC, "status": {"name": "Open"}, "labels": ["ops"],
                  "comment": {"comments": [{"author": {"displayName": "Ann"}, "body": DOC}]}}  # fmt: skip
        return httpx.Response(200, json={"fields": fields})

    docs = await collect(
        JiraConnector(
            "https://x.atlassian.net", "project = ENG", "a@b.c", "t", http=client(handler)
        )
    )
    assert [(d.key, d.version) for d in docs] == [("ENG-1", "u1"), ("ENG-2", "u2")]
    name, data = await docs[0].load()
    text = data.decode()
    assert name == "ENG-1.md" and "# ENG-1: Deploys fail" in text and "Status: Open" in text
    assert "Labels: ops" in text and "Comment by Ann:" in text and "check logs" in text


async def test_atlassian_failures_are_upstream_errors():
    down = client(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamError):
        await collect(
            ConfluenceConnector("https://x.atlassian.net", "ENG", "a@b.c", "t", http=down)
        )
    odd = client(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(UpstreamError):
        await collect(
            JiraConnector("https://x.atlassian.net", "project = X", "a@b.c", "t", http=odd)
        )


# ---------------------------------------------------------------- Notion
def test_notion_text_helpers():
    assert (
        rich_text([{"plain_text": "a"}, {"plain_text": "b"}, "junk"]) == "ab"
        and rich_text(None) == ""
    )
    page = {
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Roadmap"}]},
            "Tags": {"type": "x"},
        }
    }
    assert page_title(page) == "Roadmap" and page_title({}) == "Untitled"


async def test_notion_search_blocks_depth_and_archived_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.headers["notion-version"]
            and request.headers["authorization"] == "Bearer secret"
        )
        path = request.url.path
        if path.endswith("/search"):
            body = json.loads(request.content)
            assert body["filter"] == {"property": "object", "value": "page"}
            pages = [
                {"object": "page", "id": "p1", "last_edited_time": "t1",
                 "properties": {"T": {"type": "title", "title": [{"plain_text": "Roadmap"}]}}},
                {"object": "page", "id": "p2", "archived": True, "properties": {}},
                {"object": "database", "id": "d1"},
            ]  # fmt: skip
            return httpx.Response(200, json={"results": pages, "has_more": False})
        if path.endswith("/blocks/p1/children"):
            blocks = [
                {"id": "b1", "type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Goals"}]}},
                {"id": "b2", "type": "bulleted_list_item", "has_children": True,
                 "bulleted_list_item": {"rich_text": [{"plain_text": "ship"}]}},
            ]  # fmt: skip
            return httpx.Response(200, json={"results": blocks, "has_more": False})
        child = [
            {
                "id": "b3",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "by Friday"}]},
            }
        ]
        return httpx.Response(200, json={"results": child, "has_more": False})

    notion = NotionConnector(
        "secret",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer secret", "Notion-Version": "x"},
        ),
    )
    docs = await collect(notion)
    assert [(d.key, d.version) for d in docs] == [("p1", "t1")]
    name, data = await docs[0].load()
    assert name == "Roadmap.md" and data.decode().splitlines() == [
        "# Roadmap",
        "# Goals",
        "- ship",
        "  by Friday",
    ]


async def test_notion_database_mode_and_failures():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"results": [], "has_more": False})

    assert await collect(NotionConnector("t", database="db1", http=client(handler))) == []
    assert seen == ["/v1/databases/db1/query"]
    with pytest.raises(UpstreamError):
        await collect(NotionConnector("t", http=client(lambda r: httpx.Response(401))))


# ---------------------------------------------------------------- SharePoint
def graph_handler(posts):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "oauth2" in path:
            posts.append(request)
            return httpx.Response(200, json={"access_token": "gtok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer gtok"
        if path.endswith("/root:/Policies:/children") or path.endswith("/root/children"):
            return httpx.Response(200, json={"value": [
                {"id": "f1", "name": "Policy.pdf", "size": 10, "file": {}, "eTag": "e1"},
                {"id": "f2", "name": "movie.mp4", "size": 10, "file": {}},
                {"id": "f3", "name": "Huge.pdf", "size": 99_000_000, "file": {}},
                {"id": "sub", "name": "Team", "folder": {}},
            ], "@odata.nextLink": "https://graph.test/v1.0/page2"})  # fmt: skip
        if path.endswith("/page2"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f4",
                            "name": "notes.md",
                            "size": 5,
                            "file": {},
                            "lastModifiedDateTime": "m4",
                        }
                    ]
                },
            )
        if path.endswith("/items/sub/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "f5", "name": "deep.docx", "size": 5, "file": {}, "eTag": "e5"}
                    ]
                },
            )
        return httpx.Response(200, content=b"bytes of " + path.encode())

    return handler


async def test_sharepoint_lists_recursively_pages_and_filters_and_loads():
    posts: list[httpx.Request] = []
    http = client(graph_handler(posts))
    conn = SharePointConnector(
        "tenant-1", "cid", "secret", "drive1", graph="https://graph.test/v1.0", http=http
    )
    docs = await collect(conn)
    assert sorted(d.key for d in docs) == ["f1", "f4", "f5"]  # no video, no 99 MB file
    by = {d.key: d for d in docs}
    assert by["f1"].version == "e1" and by["f4"].version == "m4"
    name, data = await by["f1"].load()
    assert name == "Policy.pdf" and data.endswith(b"/drives/drive1/items/f1/content")
    assert (
        len(posts) == 1 and b"client_secret=secret" in posts[0].content
    )  # one token for all calls
    folder = SharePointConnector(
        "t",
        "c",
        "s",
        "drive1",
        folder="/Policies/",
        graph="https://graph.test/v1.0",
        http=client(graph_handler([])),
    )
    assert {d.key for d in await collect(folder)} >= {"f1"}


async def test_sharepoint_refuses_bad_drive_ids_and_reports_auth_failures():
    for bad in ("", "a/b", "a b", "a?b"):
        with pytest.raises(ValidationError):
            SharePointConnector("t", "c", "s", bad)
    denied = client(lambda r: httpx.Response(401))
    with pytest.raises(UpstreamError, match="refused"):
        await collect(SharePointConnector("t", "c", "s", "d", http=denied))


# ---------------------------------------------------------------- spec and factory
def test_specs_and_factory_for_the_new_types(monkeypatch):
    common = {"org": "o", "owner": "bot", "project": "p1"}
    specs = parse_specs([
        {"name": "wiki", "type": "confluence", "base_url": "https://x.atlassian.net/wiki", "space": "ENG", "email": "a@b.c", "token_env": "T1", **common},
        {"name": "bugs", "type": "jira", "base_url": "https://x.atlassian.net", "jql": "project = ENG", "email": "a@b.c", "token_env": "T1", **common},
        {"name": "plans", "type": "notion", "token_env": "T2", **common},
        {"name": "files", "type": "sharepoint", "tenant": "t", "client_id": "c", "secret_env": "T3", "drive_id": "d", **common},
    ])  # fmt: skip
    monkeypatch.setenv("T1", "tok1")
    monkeypatch.setenv("T2", "tok2")
    monkeypatch.setenv("T3", "tok3")
    built = [type(make_connector(s, Settings())).__name__ for s in specs]
    assert built == [
        "ConfluenceConnector",
        "JiraConnector",
        "NotionConnector",
        "SharePointConnector",
    ]
    monkeypatch.delenv("T2")
    with pytest.raises(ValidationError, match="T2 is not set"):
        make_connector(specs[2], Settings())
    for bad in (
        {
            "name": "x",
            "type": "jira",
            "base_url": "https://x",
            "email": "a@b.c",
            "token_env": "T",
            **common,
        },  # no jql
        {"name": "x", "type": "notion", "database": "d", **common},  # no token_env
        {
            "name": "x",
            "type": "sharepoint",
            "tenant": "t",
            "client_id": "c",
            "drive_id": "d",
            **common,
        },
        {
            "name": "x",
            "type": "confluence",
            "bogus": 1,
            "base_url": "https://x",
            "space": "E",
            "email": "a@b.c",
            "token_env": "T",
            **common,
        },
    ):
        with pytest.raises(ValidationError):
            parse_specs([bad])
