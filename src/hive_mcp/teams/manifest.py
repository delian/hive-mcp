"""The Teams app package (a zip with manifest.json and two icons), generated for this server.

It declares the bot (personal chat, teams, group chats) and the message extension: a "Search Hive"
command for the compose box and a "Save to Hive" action on any message. Upload the zip in Teams
(Apps, Manage your apps, Upload an app) or in the Teams admin center.
"""

from __future__ import annotations

import io
import json
import math
import struct
import zipfile
import zlib
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMA = "https://developer.microsoft.com/json-schemas/teams/v1.17/MicrosoftTeams.schema.json"
MANIFEST_VERSION = "1.17"
ACCENT = "#F5A524"
Pixel = tuple[int, int, int, int]


def manifest(  # noqa: PLR0913 - the package's few knobs
    app_id: str,
    public_url: str,
    *,
    name: str = "Hive",
    version: str = "1.0.0",
    privacy_url: str | None = None,
    terms_url: str | None = None,
    link_unfurling: bool = False,
) -> dict[str, object]:
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValidationError("Teams needs the server's public https address (HIVE_PUBLIC_URL)")
    if not app_id.strip():
        raise ValidationError("the bot's application (client) id is required")
    site = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "$schema": SCHEMA,
        "manifestVersion": MANIFEST_VERSION,
        "version": version,
        "id": app_id,
        "developer": {
            "name": name,
            "websiteUrl": site,
            "privacyUrl": privacy_url or f"{site}/legal/privacy",
            "termsOfUseUrl": terms_url or f"{site}/legal/terms",
        },
        "name": {"short": name, "full": f"{name}: the team's shared memory"},
        "description": {
            "short": "Search and save what your team knows.",
            "full": "Ask the team's knowledge base questions in a chat, search it from the "
            "compose box, and save any message as a note.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": ACCENT,
        "bots": [
            {
                "botId": app_id,
                "scopes": ["personal", "team", "groupChat"],
                "supportsFiles": False,
                "isNotificationOnly": False,
            }
        ],
        "composeExtensions": [
            {
                "botId": app_id,
                **_handlers(parsed.hostname, link_unfurling),
                "commands": [
                    {
                        "id": "searchHive",
                        "type": "query",
                        "title": "Search Hive",
                        "description": "Search the knowledge shared with the whole team",
                        "context": ["compose", "commandBox"],
                        "parameters": [
                            {
                                "name": "query",
                                "title": "Search",
                                "inputType": "text",
                                "description": "What are you looking for?",
                            }
                        ],
                    },
                    {
                        "id": "saveMessage",
                        "type": "action",
                        "title": "Save to Hive",
                        "description": "Keep this message as a private note",
                        "context": ["message"],
                        "fetchTask": True,
                    },
                ],
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [parsed.netloc],
    }


def _handlers(host: str | None, enabled: bool) -> dict[str, object]:
    """Pasted links to notes on this server unfurl into a card (needs the admin UI's note pages)."""
    if not enabled or not host:
        return {}
    return {"messageHandlers": [{"type": "link", "value": {"domains": [host]}}]}


def _png(size: int, pixel: Callable[[int, int], Pixel]) -> bytes:
    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _inside_hexagon(dx: float, dy: float, radius: float) -> bool:
    dx, dy = abs(dx), abs(dy)
    half = radius * math.sqrt(3) / 2
    return dy <= half and dx * math.sqrt(3) / 2 + dy / 2 <= half


def icons() -> dict[str, bytes]:
    """A white hexagon ring: on amber (color icon, 192 px) and transparent (outline, 32 px)."""

    def ring(size: int, outer: float, inner: float) -> Callable[[int, int], bool]:
        centre = (size - 1) / 2
        return lambda x, y: (
            _inside_hexagon(x - centre, y - centre, outer * size)
            and not (_inside_hexagon(x - centre, y - centre, inner * size))
        )

    big, small = ring(192, 0.36, 0.24), ring(32, 0.42, 0.28)
    amber = (0xF5, 0xA5, 0x24, 255)
    return {
        "color.png": _png(192, lambda x, y: (255, 255, 255, 255) if big(x, y) else amber),
        "outline.png": _png(32, lambda x, y: (255, 255, 255, 255) if small(x, y) else (0, 0, 0, 0)),
    }


def package(  # noqa: PLR0913 - the package's few knobs
    app_id: str,
    public_url: str,
    *,
    name: str = "Hive",
    version: str = "1.0.0",
    privacy_url: str | None = None,
    terms_url: str | None = None,
    link_unfurling: bool = False,
) -> bytes:
    built = manifest(
        app_id, public_url, name=name, version=version, privacy_url=privacy_url,
        terms_url=terms_url, link_unfurling=link_unfurling,
    )  # fmt: skip
    files = {
        "manifest.json": json.dumps(built, indent=2).encode(),
        **icons(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return buffer.getvalue()
