"""Remember, recall and curate knowledge. Every method takes the authenticated Principal."""

from __future__ import annotations

import uuid
from difflib import unified_diff
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.history import KEEP_VERSIONS
from hive_mcp.domain.item import Item, ItemPatch, ItemQuery, ItemView, Page
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.security.injection import FLAG_TAG
from hive_mcp.security.sanitize import sanitize
from hive_mcp.services.models import RememberRequest, RememberResult

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.history import ItemVersion
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.history import ItemHistory
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.indexing import IndexService
    from hive_mcp.services.quota import QuotaGuard

SELF_SERVICE_MAX_LEVEL = ScopeLevel.PROJECT
MAX_TAGS_LISTED = 500
MAX_DIFF_CHARS = 20_000


def can_share_directly(principal: Principal, scope: Scope, visibility: Visibility) -> bool:
    """Private notes and own projects are self-service; wider sharing needs an approver."""
    if principal.role >= Role.ADMIN or principal.can_approve(scope):
        return True
    return scope.level <= SELF_SERVICE_MAX_LEVEL and visibility is Visibility.SCOPE


def _require_sharing_allowed(principal: Principal, scope: Scope, visibility: Visibility) -> None:
    if not can_share_directly(principal, scope, visibility):
        raise PermissionDenied(
            f"sharing at {scope.level.name.lower()} level / {visibility} visibility needs "
            "approval; keep it private or in a project, then use kb_request_promotion"
        )


class KnowledgeService:
    def __init__(  # noqa: PLR0913 - collaborators and policy
        self,
        *,
        items: ItemRepository,
        indexing: IndexService,
        audit: AuditLog,
        clock: Clock,
        quota: QuotaGuard | None = None,
        history: ItemHistory | None = None,
    ) -> None:
        self._history = history
        self._items, self._indexing, self._audit, self._clock = items, indexing, audit, clock
        self._quota = quota

    async def _audit_event(
        self, principal: Principal, action: str, target: str, **details: str
    ) -> None:
        await self._audit.append(
            AuditEvent(principal.org_id, principal.id, action, self._clock.now(), target, details)
        )

    async def remember(self, principal: Principal, req: RememberRequest) -> RememberResult:
        scope = req.scope or Scope(ScopeLevel.PRIVATE, principal.id)
        _require_sharing_allowed(principal, scope, req.visibility)
        clean = sanitize(req.title, req.body)
        size = len(clean.title) + len(clean.body)
        if self._quota:
            await self._quota.check(principal, size)
        findings = clean.findings
        tags = normalize_tags(req.tags) | ({FLAG_TAG} if clean.flagged else set())
        now = self._clock.now()
        item = Item(
            id=uuid.uuid4().hex,
            org_id=principal.org_id,
            kind=req.kind,
            title=clean.title,
            body=clean.body,
            scope=scope,
            author_id=principal.id,
            created_at=now,
            updated_at=now,
            visibility=req.visibility,
            tags=frozenset(tags),
            source=req.source,
        )
        created = await self._items.create(principal, item, req.idempotency_key)
        indexed = True
        if created.created:
            if self._quota:
                self._quota.record(principal, size)
            indexed = await self._indexing.index_or_enqueue(created.item)
            await self._audit_event(principal, "item.create", created.item.id, kind=str(item.kind))
        return RememberResult(created.item, created.created, indexed, findings)

    async def get(self, principal: Principal, item_id: str) -> ItemView:
        return await self._items.get(principal, item_id)

    async def browse(self, principal: Principal, query: ItemQuery) -> Page:
        """Newest first, only what the caller can see."""
        return await self._items.list(principal, query)

    def _versions(self) -> ItemHistory:
        if self._history is None:
            raise ValidationError("item history is not available on this server")
        return self._history

    async def history(
        self, principal: Principal, item_id: str, limit: int = 20
    ) -> list[ItemVersion]:
        """Newest first. Versions written while the item was private to someone else stay hidden."""
        if not 1 <= limit <= KEEP_VERSIONS + 1:
            raise ValidationError(f"limit must be between 1 and {KEEP_VERSIONS + 1}")
        return await self._versions().versions(principal, item_id, limit)

    async def version(self, principal: Principal, item_id: str, revision: int) -> ItemVersion:
        return await self._versions().version(principal, item_id, revision)

    async def diff(
        self, principal: Principal, item_id: str, from_revision: int, to_revision: int | None = None
    ) -> str:
        """Unified diff (title, body) of two versions you can read; `to` defaults to the current."""
        old = await self.version(principal, item_id, from_revision)
        new = (
            await self.version(principal, item_id, to_revision)
            if to_revision is not None
            else (await self._versions().versions(principal, item_id, 1))[0]
        )
        a = f"# {old.title}\n{old.body}".splitlines()
        b = f"# {new.title}\n{new.body}".splitlines()
        lines = unified_diff(
            a, b, f"revision {old.revision}", f"revision {new.revision}", lineterm=""
        )
        return "\n".join(lines)[:MAX_DIFF_CHARS] or "(no difference)"

    async def restore(
        self, principal: Principal, item_id: str, revision: int, expected_revision: int
    ) -> Item:
        """Make the item say what an earlier version said (a new revision; nothing is rewritten)."""
        old = await self.version(principal, item_id, revision)
        current = (await self._items.get(principal, item_id)).item
        title = old.title if old.title != current.title else None
        body = old.body if old.body != current.body else None
        kind = old.kind if old.kind is not current.kind else None
        add, remove = old.tags - current.tags, current.tags - old.tags
        if not (title or body or kind or add or remove):
            raise ValidationError(f"the item already matches revision {revision}")
        patch = ItemPatch(title=title, body=body, kind=kind, add_tags=add, remove_tags=remove)
        return await self.update(principal, item_id, patch, expected_revision)

    async def tags(
        self, principal: Principal, prefix: str = "", limit: int = 100
    ) -> list[tuple[str, int]]:
        if not 1 <= limit <= MAX_TAGS_LISTED:
            raise ValidationError(f"limit must be between 1 and {MAX_TAGS_LISTED}")
        return await self._items.tag_counts(principal, prefix=prefix.strip().lower(), limit=limit)

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        add_tags, remove_tags = patch.add_tags, patch.remove_tags
        title = body = None
        if patch.title is not None or patch.body is not None:
            current = (await self._items.get(principal, item_id)).item
            checked = sanitize(
                patch.title if patch.title is not None else current.title,
                patch.body if patch.body is not None else current.body,
            )
            title = checked.title if patch.title is not None else None
            body = checked.body if patch.body is not None else None
            if checked.flagged:
                add_tags, remove_tags = add_tags | {FLAG_TAG}, remove_tags - {FLAG_TAG}
            elif FLAG_TAG in current.tags:
                add_tags, remove_tags = add_tags - {FLAG_TAG}, remove_tags | {FLAG_TAG}
        clean = ItemPatch(
            title=title,
            body=body,
            kind=patch.kind,
            status=patch.status,
            add_tags=add_tags,
            remove_tags=remove_tags,
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
