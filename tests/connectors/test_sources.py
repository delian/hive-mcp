import json

import httpx
import pytest

from hive_mcp.connectors.drive import DriveConnector
from hive_mcp.connectors.github import GithubConnector
from hive_mcp.connectors.urls import UrlConnector
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest import fetch as fetch_module
from hive_mcp.ingest.fetch import FetchPolicy
from hive_mcp.security.oauth.upstream import UpstreamError


async def collect(connector):
    return [d async for d in connector.documents()]


# ---------------------------------------------------------------- GitHub
TREE = {
    "truncated": False,
    "tree": [
        {"path": "README.md", "type": "blob", "sha": "s1", "size": 20},
        {"path": "docs/guide.md", "type": "blob", "sha": "s2", "size": 20},
        {"path": "docs/img/logo.png", "type": "blob", "sha": "s3", "size": 20},
        {"path": "docs", "type": "tree", "sha": "t"},
        {"path": "src/app.py", "type": "blob", "sha": "s4", "size": 20},
        {"path": "node_modules/x/readme.md", "type": "blob", "sha": "s5", "size": 20},
        {"path": "docs/huge.md", "type": "blob", "sha": "s6", "size": 5_000_000},
    ],
}


def gh_client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/git/trees/main"):
            return httpx.Response(200, json=TREE)
        if "/contents/" in request.url.path:
            return httpx.Response(200, content=b"# hello from " + request.url.path.encode())
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_github_lists_readable_files_and_loads_only_when_asked():
    seen = []
    conn = GithubConnector("acme/docs", ref="main", http=gh_client(seen))
    docs = await collect(conn)
    assert [d.key for d in docs] == ["README.md", "docs/guide.md", "src/app.py"]
    assert [d.version for d in docs] == ["s1", "s2", "s4"] and docs[0].tags == ("repo/acme/docs",)
    assert len(seen) == 1  # the listing only: nothing downloaded yet
    name, data = await docs[1].load()
    assert name == "guide.md" and data.startswith(
        b"# hello from /repos/acme/docs/contents/docs/guide.md"
    )


async def test_github_paths_narrow_the_listing_and_bad_input_is_refused():
    docs = await collect(
        GithubConnector("acme/docs", ref="main", paths=("docs/",), http=gh_client([]))
    )
    assert [d.key for d in docs] == ["docs/guide.md"]
    for bad in ("acme", "acme/", "/docs", "a/b/c", "a b/c", "acme/do?cs"):
        with pytest.raises(ValidationError):
            GithubConnector(bad)


async def test_github_failures_are_upstream_errors_not_partial_lists():
    def handler(request):
        return (
            httpx.Response(200, json={**TREE, "truncated": True})
            if "trees" in request.url.path
            else httpx.Response(500)
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError, match="too large"):
        await collect(GithubConnector("acme/docs", ref="main", http=client))
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(UpstreamError, match="could not list"):
        await collect(GithubConnector("acme/docs", ref="main", http=down))
    docs = await collect(GithubConnector("acme/docs", ref="main", http=gh_client([])))
    broken = GithubConnector(
        "acme/docs",
        ref="main",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    broken_doc = broken._loader("README.md")
    with pytest.raises(UpstreamError, match="could not download"):
        await broken_doc()
    assert docs


def test_github_sends_the_token_only_in_the_authorization_header():
    conn = GithubConnector("acme/docs", token="ghp_secret")
    assert conn._http.headers["authorization"] == "Bearer ghp_secret"
    assert "authorization" not in GithubConnector("acme/docs")._http.headers


# ---------------------------------------------------------------- URLs
async def test_url_connector_fetches_each_address_with_the_policy(monkeypatch):
    calls = []

    async def fake_fetch(url, policy, **_):
        calls.append((url, policy))
        return fetch_module.Fetched("page.html", f"<h1>{url}</h1>".encode(), url)

    monkeypatch.setattr("hive_mcp.connectors.urls.fetch", fake_fetch)
    policy = FetchPolicy(allowed_hosts=frozenset({"wiki.acme.com"}))
    docs = await collect(
        UrlConnector(["https://wiki.acme.com/a", "https://wiki.acme.com/b"], policy)
    )
    assert [d.key for d in docs] == ["https://wiki.acme.com/a", "https://wiki.acme.com/b"]
    assert calls == []  # lazy
    name, data = await docs[0].load()
    assert name == "page.html" and b"/a" in data and calls[0][1] is policy


# ---------------------------------------------------------------- Drive
class FakeAuth:
    def __init__(self, http):
        self.http = http

    async def get(self):
        return "token-123"


FILES = {
    "root": [
        {
            "id": "f1",
            "name": "Handbook",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "t1",
        },
        {
            "id": "f2",
            "name": "Budget",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "modifiedTime": "t2",
        },
        {
            "id": "f3",
            "name": "Policy.pdf",
            "mimeType": "application/pdf",
            "md5Checksum": "m3",
            "size": "100",
        },
        {"id": "f4", "name": "holiday.mp4", "mimeType": "video/mp4", "size": "100"},
        {
            "id": "f5",
            "name": "Huge.pdf",
            "mimeType": "application/pdf",
            "size": str(50 * 1024 * 1024),
        },
        {"id": "sub", "name": "Team", "mimeType": "application/vnd.google-apps.folder"},
    ],
    "sub": [
        {
            "id": "f6",
            "name": "notes.md",
            "mimeType": "text/markdown",
            "modifiedTime": "t6",
            "size": "10",
        }
    ],
}


def drive(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer token-123"
        path = request.url.path
        if path.endswith("/files"):
            folder = request.url.params["q"].split("'")[1]
            return httpx.Response(200, json={"files": FILES[folder]})
        if path.endswith("/export"):
            return httpx.Response(
                200, content=b"exported as " + request.url.params["mimeType"].encode()
            )
        return httpx.Response(200, content=b"raw bytes of " + path.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DriveConnector("root", FakeAuth(client), api_base="https://drive.test/v3")


async def test_drive_lists_recursively_exports_google_formats_and_skips_the_unreadable():
    seen = []
    docs = await collect(drive(seen))
    assert sorted(d.key for d in docs) == ["f1", "f2", "f3", "f6"]  # no video, no 50 MB file
    by = {d.key: d for d in docs}
    assert by["f1"].version == "t1" and by["f3"].version == "m3"  # a checksum beats a time
    assert await by["f1"].load() == ("Handbook.txt", b"exported as text/plain")
    assert await by["f2"].load() == ("Budget.csv", b"exported as text/csv")
    name, data = await by["f3"].load()
    assert name == "Policy.pdf" and data.endswith(b"/files/f3")
    assert [r.url.params.get("alt") for r in seen if r.url.path.endswith("/f3")] == ["media"]


async def test_drive_refuses_bad_folder_ids_and_reports_failures():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    for bad in ("", "a'b", "a b", "a/b"):
        with pytest.raises(UpstreamError):
            DriveConnector(bad, FakeAuth(http))
    with pytest.raises(UpstreamError, match="Drive request failed"):
        await collect(DriveConnector("root", FakeAuth(http)))


async def test_the_service_account_token_exchange_and_cache(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from hive_mcp.security.google_auth import ServiceAccountToken

    key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    posts = []

    def handler(request):
        posts.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = ServiceAccountToken(
        {"client_email": "sa@x.iam", "private_key": key}, "reader@acme.com", "scope-x",
        token_uri="https://token.test/", http=http,
    )  # fmt: skip
    assert await auth.get() == "tok" and await auth.get() == "tok"
    assert len(posts) == 1 and posts[0]["grant_type"].endswith("jwt-bearer")
    import jwt as pyjwt

    claims = pyjwt.decode(posts[0]["assertion"], options={"verify_signature": False})
    assert (claims["sub"], claims["scope"], claims["aud"]) == (
        "reader@acme.com",
        "scope-x",
        "https://token.test/",
    )
    denied = ServiceAccountToken(
        {"client_email": "sa@x.iam", "private_key": key}, "r@acme.com", "s",
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )  # fmt: skip
    with pytest.raises(UpstreamError, match="refused"):
        await denied.get()
    with pytest.raises(UpstreamError, match="lacks"):
        ServiceAccountToken({"client_email": "x"}, "r@acme.com", "s")
    assert json.dumps(posts[0])
