"""Run one sync: read every mapped group, plan, and (unless dry-run) write the directory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.groups.mapping import Change, plan

if TYPE_CHECKING:
    from hive_mcp.security.directory import UserDirectory
    from hive_mcp.security.groups.client import GoogleGroups
    from hive_mcp.security.groups.mapping import GroupMap

log = logging.getLogger("hive.security")


async def sync_groups(
    client: GoogleGroups,
    mapping: GroupMap,
    directory: UserDirectory,
    *,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> list[Change]:
    """All-or-nothing: if any group cannot be read, or comes back empty (usually a typo or an API
    problem that would strip everyone of that group's access), nothing is changed."""
    members = {}
    for group in mapping.groups:
        members[group] = await client.members(group)
        if not members[group] and not allow_empty:
            raise ValidationError(
                f"{group} returned no members; refusing to sync (use --allow-empty)"
            )
    changes = plan(mapping, members, directory.list())
    if not dry_run:
        for change in changes:
            directory.upsert(change.after)
            log.info("groups.sync action=%s user=%s", change.action, change.email)
    return changes
