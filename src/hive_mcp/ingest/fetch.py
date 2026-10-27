"""Fetching a document from a URL on behalf of a user, without becoming a way into the network.

A server that fetches URLs can be aimed at things only the server can reach (cloud metadata at
169.254.169.254, internal admin pages). So, for every hop of every redirect:
- only http(s) and no credentials in the URL;
- the host name is resolved here and EVERY resulting address must be public (unless the operator
  allowed private ones); the connection then goes to that checked address, so a DNS answer that
  changes between "check" and "connect" cannot redirect us (TLS still verifies the certificate
  against the original host name);
- an optional allow-list of hosts; a size cap while streaming; a time cap; a content-type filter.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx

from hive_mcp import __version__
from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECTS = {301, 302, 303, 307, 308}
GENERIC_NAMES = {"text/html": ".html", "application/pdf": ".pdf", "text/plain": ".txt",
                 "text/markdown": ".md", "application/json": ".json", "text/csv": ".csv",
                 "application/xml": ".xml", "text/xml": ".xml", "image/png": ".png",
                 "image/jpeg": ".jpg", "image/tiff": ".tiff", "image/bmp": ".bmp",
                 "image/gif": ".gif", "image/webp": ".webp"}  # fmt: skip
OFFICE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    allow_private: bool = False  # let the server reach private addresses (an intranet wiki)
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)  # empty = any public host
    max_bytes: int = MAX_BYTES
    timeout: float = 15.0  # per network operation
    deadline: float = 60.0  # for the whole fetch: a server dribbling bytes cannot hold us forever


@dataclass(frozen=True, slots=True)
class Fetched:
    name: str  # a file name whose extension tells the extractor what it is
    data: bytes
    url: str  # final URL after redirects


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", maxsplit=1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if (
            embedded is not None
        ):  # ::ffff:10.0.0.1, 64:ff9b::10.0.0.1 and 2002:0a00:1:: all reach 10.0.0.1
            return is_public(str(embedded))
    return ip.is_global and not ip.is_multicast


NAT64 = ipaddress.ip_network("64:ff9b::/96")
SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _host_allowed(host: str, allowed: frozenset[str]) -> bool:
    return not allowed or any(host == a or host.endswith("." + a) for a in allowed)


def _check_url(url: str, policy: FetchPolicy) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValidationError("only http and https URLs can be fetched")
    if parts.username or parts.password:
        raise ValidationError("URLs with embedded credentials are not accepted")
    host = parts.hostname.lower()
    if not _host_allowed(host, policy.allowed_hosts):
        raise ValidationError(f"{host} is not on this server's list of allowed hosts")
    return host, parts.port or (443 if parts.scheme == "https" else 80)


async def _pin(
    host: str, policy: FetchPolicy, resolve: Callable[[str], Awaitable[list[str]]]
) -> str:
    try:
        addresses = [host] if _is_literal(host) else await resolve(host)
    except OSError as exc:
        raise ValidationError(f"cannot resolve {host}") from exc
    if not addresses:
        raise ValidationError(f"cannot resolve {host}")
    if not policy.allow_private and not all(is_public(a) for a in addresses):
        raise ValidationError(f"{host} resolves to a non-public address; refusing to fetch it")
    return addresses[0]


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _file_name(url: str, content_type: str) -> str:
    parts = urlsplit(url)
    path = PurePosixPath(parts.path or "/")
    stem = "/".join(path.parts[1:]).removesuffix(path.suffix) or "index"
    suffix = path.suffix.lower()
    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    if not suffix or suffix in {".php", ".aspx"}:  # the URL does not say what it is: ask the server
        suffix = GENERIC_NAMES.get(mime) or OFFICE_TYPES.get(mime) or ".html"
    return f"{parts.hostname}/{stem}{suffix}"[:200]


def _acceptable(content_type: str) -> bool:
    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    return (
        not mime
        or mime.startswith("text/")
        or mime in GENERIC_NAMES
        or mime in OFFICE_TYPES
        or mime.endswith(("+xml", "+json"))
        or mime == "application/octet-stream"
    )


async def fetch(
    url: str,
    policy: FetchPolicy,
    *,
    resolve: Callable[[str], Awaitable[list[str]]] = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Fetched:
    try:
        async with asyncio.timeout(policy.deadline):
            return await _fetch(url, policy, resolve, transport)
    except TimeoutError as exc:
        raise ValidationError(f"fetching took longer than {policy.deadline:.0f} seconds") from exc


async def _fetch(
    url: str,
    policy: FetchPolicy,
    resolve: Callable[[str], Awaitable[list[str]]],
    transport: httpx.AsyncBaseTransport | None,
) -> Fetched:
    current = url
    async with httpx.AsyncClient(
        transport=transport, timeout=policy.timeout, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host, port = _check_url(current, policy)
            address = await _pin(host, policy, resolve)
            parts = urlsplit(current)
            netloc = f"[{address}]" if ":" in address else address
            target = parts._replace(netloc=f"{netloc}:{port}").geturl()
            headers = {
                "Host": parts.netloc,
                "User-Agent": f"hive-mcp/{__version__}",
                "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5",
            }
            request = client.build_request(
                "GET", target, headers=headers, extensions={"sni_hostname": host}
            )  # TLS checks the certificate against the real host name
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise ValidationError(f"could not fetch {host}: {exc.__class__.__name__}") from exc
            try:
                if response.status_code in REDIRECTS and response.headers.get("location"):
                    current = urljoin(current, response.headers["location"])
                    continue
                return await _read(response, current, policy)
            finally:
                await response.aclose()
    raise ValidationError(f"too many redirects (more than {MAX_REDIRECTS})")


async def _read(response: httpx.Response, url: str, policy: FetchPolicy) -> Fetched:
    if response.status_code != httpx.codes.OK:
        raise ValidationError(f"the server answered HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if not _acceptable(content_type):
        raise ValidationError(f"content type {content_type.split(';')[0]!r} is not a document")
    declared = response.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > policy.max_bytes:
        raise ValidationError(f"the document is larger than {policy.max_bytes // (1024 * 1024)} MB")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(65536):
        total += len(chunk)
        if total > policy.max_bytes:
            raise ValidationError(
                f"the document is larger than {policy.max_bytes // (1024 * 1024)} MB"
            )
        chunks.append(chunk)
    return Fetched(_file_name(url, content_type), b"".join(chunks), url)
