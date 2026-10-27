"""Builds connectors from their specs, and runs them on a schedule inside the server process."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from hive_mcp.connectors.drive import DriveConnector
from hive_mcp.connectors.github import API as GITHUB_API
from hive_mcp.connectors.github import GithubConnector
from hive_mcp.connectors.urls import UrlConnector
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.fetch import FetchPolicy
from hive_mcp.security.google_auth import ServiceAccountToken

if TYPE_CHECKING:
    from hive_mcp.config import Settings
    from hive_mcp.connectors.base import Connector
    from hive_mcp.connectors.spec import ConnectorSpec


def make_connector(spec: ConnectorSpec, settings: Settings) -> Connector:
    o = spec.options
    if spec.type == "urls":
        policy = FetchPolicy(
            allow_private=settings.ingest_url_allow_private,
            allowed_hosts=frozenset(h.lower() for h in settings.ingest_url_hosts),
        )
        return UrlConnector(list(o["urls"]), policy)
    if spec.type == "github":
        env = o.get("token_env")
        token = os.environ.get(str(env)) if env else None
        if env and not token:
            raise ValidationError(f"connector {spec.name}: environment variable {env} is not set")
        return GithubConnector(
            str(o["repo"]), ref=str(o.get("ref", "HEAD")), paths=tuple(o.get("paths", ())),
            token=token, api_base=str(o.get("api_base") or GITHUB_API),
        )  # fmt: skip
    try:
        key = json.loads(Path(str(o["key_file"])).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"connector {spec.name}: cannot read the key file: {exc}") from exc
    auth = ServiceAccountToken(
        key, str(o["as_user"]), "https://www.googleapis.com/auth/drive.readonly"
    )
    return DriveConnector(str(o["folder"]), auth)
