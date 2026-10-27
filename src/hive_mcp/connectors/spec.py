"""Connector settings (HIVE_CONNECTORS): validated at startup, secrets by reference only.

[{"name": "handbook", "type": "drive", "folder": "1AbC...", "key_file": "/run/secrets/drive.json",
  "as_user": "docs-reader@acme.com", "org": "acme", "owner": "connector-bot", "project": "p1",
  "tags": ["source/handbook"], "every_minutes": 360}]
Secrets are never written into the setting itself: a key file path, or the NAME of an environment
variable (`token_env`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.tags import normalize_tags

TYPES = {
    "urls": {"urls"},
    "github": {"repo", "ref", "paths", "token_env", "api_base"},
    "drive": {"folder", "key_file", "as_user"},
}
COMMON = {"name", "type", "org", "owner", "project", "tags", "every_minutes"}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MIN_MINUTES = 5
DEFAULT_MINUTES = 360


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    name: str
    type: str
    org: str
    owner: str  # the identity that authors the imported items
    project: str  # the project that can read them
    tags: tuple[str, ...] = ()
    every_minutes: int = DEFAULT_MINUTES
    options: dict[str, Any] = field(default_factory=dict)


def parse_specs(raw: list[dict[str, Any]]) -> list[ConnectorSpec]:
    specs: list[ConnectorSpec] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("type") not in TYPES:
            raise ValidationError(f"connector {entry!r}: type must be one of {sorted(TYPES)}")
        kind = str(entry["type"])
        unknown = set(entry) - COMMON - TYPES[kind]
        name = str(entry.get("name", ""))
        if unknown or not NAME.fullmatch(name):
            raise ValidationError(
                f"connector {name or entry!r}: a lowercase name is required"
                + (f" and {sorted(unknown)} are not options of {kind}" if unknown else "")
            )
        if any(s.name == name for s in specs):
            raise ValidationError(f"connector name {name!r} is used twice")
        for required in ("org", "owner", "project"):
            if not str(entry.get(required, "")).strip():
                raise ValidationError(f"connector {name}: {required} is required")
        minutes = int(entry.get("every_minutes", DEFAULT_MINUTES))
        if minutes < MIN_MINUTES:
            raise ValidationError(f"connector {name}: every_minutes must be at least {MIN_MINUTES}")
        options = {k: v for k, v in entry.items() if k in TYPES[kind]}
        _check_options(name, kind, options)
        tags = tuple(sorted(normalize_tags(list(entry.get("tags", [])))))
        specs.append(
            ConnectorSpec(
                name, kind, str(entry["org"]), str(entry["owner"]), str(entry["project"]), tags,
                minutes, options,
            )
        )  # fmt: skip
    return specs


def _check_options(name: str, kind: str, options: dict[str, Any]) -> None:
    need = {"urls": ("urls",), "github": ("repo",), "drive": ("folder", "key_file", "as_user")}
    for key in need[kind]:
        if not options.get(key):
            raise ValidationError(f"connector {name}: {key} is required for {kind}")
    if kind == "urls" and (
        not isinstance(options["urls"], list)
        or not all(isinstance(u, str) for u in options["urls"])
    ):
        raise ValidationError(f"connector {name}: urls must be a list of addresses")
    if kind == "github" and "paths" in options and not isinstance(options["paths"], list):
        raise ValidationError(f"connector {name}: paths must be a list")
