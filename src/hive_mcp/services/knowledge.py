"""Remember, recall and curate knowledge. Every method takes the authenticated Principal."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import PermissionDenied
from hive_mcp.domain.item import Item, ItemPatch, ItemView
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.redact import redact
from hive_mcp.services.models import RememberRequest, RememberResult

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.indexing import IndexService

SELF_SERVICE_MAX_LEVEL = ScopeLevel.PROJECT


def can_share_directly(principal: Principal, scope: Scope, visibility: Visibility) -> bool:
    """Private notes and own projects are self-service; wider sharing needs an approver."""
    if principal.role >= Role.ADMIN:
        return True
    return scope.level <= SELF_SERVICE_MAX_LEVEL and visibility is Visibility.SCOPE


def _require_sharing_allowed(principal: Principal, scope: Scope, visibility: Visibility) -> None:
    if not can_share_directly(principal, scope, visibility):
        raise PermissionDenied(
            f"sharing at {scope.level.name.lower()} level / {visibility} visibility needs "
            "approval; "
            "keep it private or in a project, and ask an approver to promote it"
        )


class KnowledgeService:
    def __init__(
        self, *, items: ItemRepository, indexing: IndexService, audit: AuditLog, clock: Clock
    ) -> None:
        self._items, self._indexing, self._audit, self._clock = items, indexing, audit, clock

    async def _audit_event(
        self, principal: Principal, action: str, target: str, **details: str
    ) -> None:
        await self._audit.append(
            AuditEvent(principal.org_id, principal.id, action, self._clock.now(), target, details)
        )

    async def remember(self, principal: Principal, req: RememberRequest) -> RememberResult:
        scope = req.scope or Scope(ScopeLevel.PRIVATE, principal.id)
        _require_sharing_allowed(principal, scope, req.visibility)
        title, body = redact(req.title), redact(req.body)
        findings = {
            k: title.findings.get(k, 0) + body.findings.get(k, 0)
            for k in {*title.findings, *body.findings}
        }
        now = self._clock.now()
        item = Item(
            id=uuid.uuid4().hex,
            org_id=principal.org_id,
            kind=req.kind,
            title=title.text,
            body=body.text,
            scope=scope,
            author_id=principal.id,
            created_at=now,
            updated_at=now,
            visibility=req.visibility,
            tags=normalize_tags(req.tags),
            source=req.source,
        )
        created = await self._items.create(principal, item, req.idempotency_key)
        indexed = True
        if created.created:
            indexed = await self._indexing.index_or_enqueue(created.item)
            await self._audit_event(principal, "item.create", created.item.id, kind=str(item.kind))
        return RememberResult(created.item, created.created, indexed, findings)

    async def get(self, principal: Principal, item_id: str) -> ItemView:
        return await self._items.get(principal, item_id)

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        title = redact(patch.title) if patch.title is not None else None
        body = redact(patch.body) if patch.body is not None else None
        clean = ItemPatch(
            title=title.text if title else None,
            body=body.text if body else None,
            kind=patch.kind,
            status=patch.status,
            add_tags=patch.add_tags,
            remove_tags=patch.remove_tags,
        )
        item = await self._items.update(principal, item_id, clean, expected_revision)
        await self._indexing.index_or_enqueue(item)
        await self._audit_event(principal, "item.update", item_id, revision=str(item.revision))
        return item

    async def tag(
        self,
        principal: Principal,
        item_id: str,
        add: list[str],
        remove: list[str],
        expected_revision: int,
    ) -> Item:
        patch = ItemPatch(add_tags=normalize_tags(add), remove_tags=normalize_tags(remove))
        return await self.update(principal, item_id, patch, expected_revision)

    async def move(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        """Promote or demote; the indexes copy the access rules, so they are updated too."""
        _require_sharing_allowed(principal, scope, visibility)
        item = await self._items.relocate(principal, item_id, scope, visibility, expected_revision)
        await self._indexing.sync_acl(item)
        await self._audit_event(
            principal,
            "item.relocate",
            item_id,
            scope=f"{scope.level.name.lower()}:{scope.owner}",
            visibility=str(visibility),
        )
        return item


__all__ = ["KnowledgeService", "can_share_directly"]
