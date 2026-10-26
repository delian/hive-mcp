"""Builds the group sync from settings (None when it is not configured)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.groups.client import GoogleGroups
from hive_mcp.security.groups.mapping import GroupMap
from hive_mcp.security.groups.sync import sync_groups
from hive_mcp.security.jsonfile import AtomicJsonFile

if TYPE_CHECKING:
    from hive_mcp.config import Settings
    from hive_mcp.security.groups.mapping import Change


def group_sync_configured(settings: Settings) -> bool:
    return bool(settings.group_map_file)


async def run_group_sync(
    settings: Settings, *, dry_run: bool = False, allow_empty: bool = False
) -> list[Change]:
    if not (settings.group_map_file and settings.group_sync_key_file and settings.group_sync_admin):
        raise ValidationError(
            "group sync needs HIVE_GROUP_MAP_FILE, HIVE_GROUP_SYNC_KEY_FILE, HIVE_GROUP_SYNC_ADMIN"
        )
    try:
        mapping = GroupMap.parse(
            json.loads(settings.group_map_file.read_text()), settings.google_allowed_domains
        )
        key = json.loads(settings.group_sync_key_file.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"cannot read group sync files: {exc}") from exc
    client = GoogleGroups(key, settings.group_sync_admin)
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
    return await sync_groups(client, mapping, directory, dry_run=dry_run, allow_empty=allow_empty)
