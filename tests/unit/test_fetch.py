import asyncio
import ipaddress

import httpx
import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.fetch import FetchPolicy, _file_name, fetch, is_public

PUBLIC = "93.184.216.34"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC, True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
        ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.5.5", False), ("192.168.1.1", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False),  # noqa: S104
        ("224.0.0.1", False),
        ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False),
        ("::ffff:127.0.0.1", False), ("fe80::1%eth0", False), ("::ffff:8.8.8.8", True),
        ("64:ff9b::a00:1", False), ("64:ff9b::7f00:1", False), ("64:ff9b::a9fe:a9fe", False),  # NAT64 to private
        ("64:ff9b::808:808", True),
        ("2002:a00:1::", False), ("2002:7f00:1::", False), ("2002:808:808::", True),  # 6to4 to private
    ],
)  # fmt: skip
def test_which_addresses_count_as_public(address, public):
    assert is_public(address) is public


class Recorder:
    """A fake network: records requests and answers from a table of {ip-or-host: response}."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes[request.url.host]
        return answer(request) if callable(answer) else answer


def resolver(table):
    async def resolve(host):
        return table[host]

    return resolve


async def test_connects_to_the_checked_address_and_keeps_the_host_name_for_http_and_tls():
    page = httpx.Response(
        200,
        content=b"<html><title>T</title><body>hello</body></html>",
        headers={"content-type": "text/html"},
    )
    rec = Recorder({PUBLIC: page})
    result = await fetch(
        "https://example.org/docs/page?x=1",
        FetchPolicy(),
        resolve=resolver({"example.org": [PUBLIC]}),
        transport=httpx.MockTransport(rec.handler),
    )
    (request,) = rec.requests
    assert request.url.host == PUBLIC and request.headers["host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"
    assert str(request.url).endswith("/docs/page?x=1")
    assert result.name == "example.org/docs/page.html" and result.data.startswith(b"<html>")


async def test_private_destinations_are_refused_by_name_literal_and_odd_notations():
    rec = Recorder({})
    policy = FetchPolicy()
    cases = {
        "http://metadata.internal/latest": {"metadata.internal": ["169.254.169.254"]},
        "http://localhost/admin": {"localhost": ["127.0.0.1", "::1"]},
        "http://169.254.169.254/latest/meta-data": {},
        "http://127.0.0.1:8080/": {},
        "http://[::1]/": {},
        "http://2130706433/": {"2130706433": ["127.0.0.1"]},  # decimal form of 127.0.0.1
        "http://mixed.example/": {
            "mixed.example": [PUBLIC, "10.0.0.5"]
        },  # one bad answer is enough
    }
    for url, dns in cases.items():
        with pytest.raises(ValidationError):
            await fetch(
                url, policy, resolve=resolver(dns), transport=httpx.MockTransport(rec.handler)
            )
    assert rec.requests == []  # nothing was even sent


async def test_operators_can_allow_private_hosts_and_restrict_to_a_list():
    rec = Recorder(
        {
            "10.0.0.7": httpx.Response(
                200, content=b"intranet page", headers={"content-type": "text/plain"}
            )
        }
    )
    ok = await fetch(
        "http://wiki.corp/a.txt",
        FetchPolicy(allow_private=True),
        resolve=resolver({"wiki.corp": ["10.0.0.7"]}),
        transport=httpx.MockTransport(rec.handler),
    )
    assert ok.data == b"intranet page"
    listed = FetchPolicy(allowed_hosts=frozenset({"example.org"}))
    for url in (
        "https://evil.example.net/",
        "https://notexample.org/",
        "https://example.org.evil.net/",
    ):
        with pytest.raises(ValidationError, match="allowed hosts"):
            await fetch(
                url, listed, resolve=resolver({}), transport=httpx.MockTransport(rec.handler)
            )
    rec2 = Recorder(
        {PUBLIC: httpx.Response(200, content=b"x y", headers={"content-type": "text/plain"})}
    )
    sub = await fetch(
        "https://docs.example.org/",
        listed,
        resolve=resolver({"docs.example.org": [PUBLIC]}),
        transport=httpx.MockTransport(rec2.handler),
    )
    assert sub.data == b"x y"  # subdomains of an allowed host are fine


async def test_every_redirect_hop_is_checked_again():
    routes = {PUBLIC: httpx.Response(302, headers={"location": "http://internal.example/secret"})}
    rec = Recorder(routes)
    with pytest.raises(ValidationError, match="non-public"):
        await fetch(
            "https://start.example/",
            FetchPolicy(),
            resolve=resolver({"start.example": [PUBLIC], "internal.example": ["192.168.0.9"]}),
            transport=httpx.MockTransport(rec.handler),
        )
    assert len(rec.requests) == 1  # the second hop was never contacted
    loop = Recorder({PUBLIC: httpx.Response(302, headers={"location": "/again"})})
    with pytest.raises(ValidationError, match="too many redirects"):
        await fetch(
            "https://loop.example/",
            FetchPolicy(),
            resolve=resolver({"loop.example": [PUBLIC]}),
            transport=httpx.MockTransport(loop.handler),
        )
    with pytest.raises(ValidationError, match="only http"):
        await fetch("file:///etc/passwd", FetchPolicy(), resolve=resolver({}))
    with pytest.raises(ValidationError, match="only http"):
        await fetch("ftp://example.org/x", FetchPolicy(), resolve=resolver({}))
    with pytest.raises(ValidationError, match="credentials"):
        await fetch(
            "https://user:pw@example.org/",
            FetchPolicy(),
            resolve=resolver({"example.org": [PUBLIC]}),
        )
    hop = Recorder({PUBLIC: httpx.Response(302, headers={"location": "file:///etc/passwd"})})
    with pytest.raises(ValidationError, match="only http"):
        await fetch(
            "https://start.example/",
            FetchPolicy(),
            resolve=resolver({"start.example": [PUBLIC]}),
            transport=httpx.MockTransport(hop.handler),
        )


async def test_size_status_and_content_type_limits():
    policy = FetchPolicy(max_bytes=100)
    dns = resolver({"example.org": [PUBLIC]})

    async def get(response):
        rec = Recorder({PUBLIC: response})
        return await fetch(
            "https://example.org/f", policy, resolve=dns, transport=httpx.MockTransport(rec.handler)
        )

    class Endless(httpx.AsyncByteStream):
        """No Content-Length: only counting while reading can stop it."""

        async def __aiter__(self):
            for _ in range(1000):
                yield b"x" * 50

    with pytest.raises(ValidationError, match="larger"):
        await get(httpx.Response(200, stream=Endless(), headers={"content-type": "text/plain"}))
    with pytest.raises(ValidationError, match="larger"):
        await get(httpx.Response(200, content=b"x" * 101, headers={"content-type": "text/plain"}))
    with pytest.raises(ValidationError, match="larger"):
        await get(
            httpx.Response(
                200, content=b"x", headers={"content-type": "text/plain", "content-length": "5000"}
            )
        )
    with pytest.raises(ValidationError, match="HTTP 404"):
        await get(httpx.Response(404, content=b"nope"))
    with pytest.raises(ValidationError, match="not a document"):
        await get(httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    assert (
        await get(httpx.Response(200, content=b"x" * 100, headers={"content-type": "text/plain"}))
    ).data == b"x" * 100


async def test_network_failures_become_validation_errors():
    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ValidationError, match="could not fetch"):
        await fetch(
            "https://example.org/",
            FetchPolicy(),
            resolve=resolver({"example.org": [PUBLIC]}),
            transport=httpx.MockTransport(boom),
        )

    async def nxdomain(host):
        raise OSError("no such host")

    with pytest.raises(ValidationError, match="cannot resolve"):
        await fetch("https://nope.invalid/", FetchPolicy(), resolve=nxdomain)


def test_file_names_get_an_extension_the_extractor_understands():
    assert _file_name("https://e.org/a/b.pdf", "application/pdf") == "e.org/a/b.pdf"
    assert _file_name("https://e.org/", "text/html; charset=utf-8") == "e.org/index.html"
    assert _file_name("https://e.org/article", "text/plain") == "e.org/article.txt"
    assert _file_name("https://e.org/view.php", "text/html") == "e.org/view.html"
    assert (
        _file_name(
            "https://e.org/dl",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        == "e.org/dl.docx"
    )
    assert str(ipaddress.ip_address("::1")) == "::1"


async def test_a_server_that_dribbles_data_is_cut_off_by_the_overall_deadline():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b"x"

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Slow(), headers={"content-type": "text/plain"})
    )
    with pytest.raises(ValidationError, match="took longer"):
        await fetch(
            "https://example.org/",
            FetchPolicy(deadline=0.2),
            resolve=resolver({"example.org": [PUBLIC]}),
            transport=transport,
        )
