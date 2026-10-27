"""Knowledge items and the pure functions every adapter shares (patching, hashing, query match)."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from hive_mcp.domain.access import Acl, Tier
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Scope, Visibility, check_visibility
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.domain.text import check_text, has_bad_text

MAX_TITLE = 200
MAX_BODY = 200_000
MAX_PAGE = 100


class Kind(StrEnum):
    NOTE = "note"
    RESEARCH = "research"
    FORMULA = "formula"
    MEASUREMENT = "measurement"
    DECISION = "decision"
    BEST_PRACTICE = "best_practice"
    DOC = "doc"
    HOWTO = "howto"
    SESSION = "session"
    COMPACTION = "compaction"
    ENTITY = "entity"  # a person, system, part or concept other notes can point at


class Status(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # kept for history, down-ranked
    ARCHIVED = "archived"  # soft-deleted, hidden by default


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def content_hash(kind: Kind, title: str, body: str) -> str:
    """Dedupe key for identical knowledge (tags/scope do not change what was said)."""
    return hashlib.sha256(f"{kind}\0{_nfc(title)}\0{_nfc(body)}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    org_id: str
    kind: Kind
    title: str
    body: str
    scope: Scope
    author_id: str
    created_at: datetime
    updated_at: datetime
    visibility: Visibility = Visibility.SCOPE
    status: Status = Status.ACTIVE
    tags: frozenset[str] = field(default_factory=frozenset)
    revision: int = 1
    supersedes: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.org_id or not self.author_id:
            raise ValidationError("id, org_id and author_id are required")
        if not self.title.strip() or len(self.title) > MAX_TITLE:
            raise ValidationError(f"title must be 1-{MAX_TITLE} characters")
        if not self.body.strip() or len(self.body) > MAX_BODY:
            raise ValidationError(f"body must be 1-{MAX_BODY} characters")
        texts = (
            self.id,
            self.org_id,
            self.author_id,
            self.title,
            self.body,
            self.scope.owner,
            self.supersedes or "",
            self.source or "",
        )
        if has_bad_text(*texts):
            raise ValidationError("text must not contain NUL characters or lone surrogates")
        if self.tags != normalize_tags(self.tags):
            raise ValidationError("tags must be normalised with normalize_tags()")
        if self.revision < 1:
            raise ValidationError("revision starts at 1")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValidationError("timestamps must be timezone-aware (UTC)")
        check_visibility(self.scope, self.visibility)

    @property
    def acl(self) -> Acl:
        return Acl(self.org_id, self.scope, self.visibility)

    @property
    def content_hash(self) -> str:
        return content_hash(self.kind, self.title, self.body)

    def fingerprint(self) -> str:
        """Hash of every caller-supplied field; detects idempotency-key reuse with a new payload."""
        parts = [
            self.content_hash,
            ",".join(sorted(self.tags)),
            str(self.scope.level),
            self.scope.owner,
            self.visibility,
            self.status,
            self.supersedes or "",
            self.source or "",
        ]
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ItemPatch:
    """Partial update. Tags are add/remove sets: concurrent edits cannot drop each other's tags."""

    title: str | None = None
    body: str | None = None
    kind: Kind | None = None
    status: Status | None = None
    add_tags: frozenset[str] = field(default_factory=frozenset)
    remove_tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        check_text(self.title or "", self.body or "")
        if self.add_tags & self.remove_tags:
            raise ValidationError("a tag cannot be both added and removed")
        if self.add_tags != normalize_tags(self.add_tags) or self.remove_tags != normalize_tags(
            self.remove_tags
        ):
            raise ValidationError("tags must be normalised with normalize_tags()")
        if self.is_empty:
            raise ValidationError("empty patch")

    @property
    def is_empty(self) -> bool:
        return not (
            self.title or self.body or self.kind or self.status or self.add_tags or self.remove_tags
        )


def apply_patch(item: Item, patch: ItemPatch, now: datetime) -> Item:
    """The one place a revision is produced (re-validates through Item.__post_init__)."""
    return replace(
        item,
        title=patch.title if patch.title is not None else item.title,
        body=patch.body if patch.body is not None else item.body,
        kind=patch.kind or item.kind,
        status=patch.status or item.status,
        tags=(item.tags - patch.remove_tags) | patch.add_tags,
        revision=item.revision + 1,
        updated_at=now,
    )


def relocate_item(item: Item, scope: Scope, visibility: Visibility, now: datetime) -> Item:
    """Promotion/demotion transform; re-validates invariants (private cannot be org-visible)."""
    return replace(
        item, scope=scope, visibility=visibility, revision=item.revision + 1, updated_at=now
    )


@dataclass(frozen=True, slots=True)
class Cursor:
    """Keyset pagination position (newest first): items strictly older than (time, id), where the
    time is the creation time, or the last-change time when the query sorts by that."""

    created_at: datetime
    id: str

    def encode(self) -> str:
        raw = json.dumps([self.created_at.isoformat(), self.id]).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @staticmethod
    def decode(token: str) -> Cursor:
        try:
            ts, ident = json.loads(base64.urlsafe_b64decode(token.encode()))
            when = datetime.fromisoformat(ts)
            if when.tzinfo is None:  # naive datetimes cannot be compared with stored UTC timestamps
                raise ValueError("naive timestamp")
            when = when.astimezone(UTC)  # out-of-range instants overflow here, not in a database
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise ValidationError("invalid cursor") from exc
        if has_bad_text(str(ident)):
            raise ValidationError("invalid cursor")
        return Cursor(when, str(ident))

    def before(self, item: Item, by_updated: bool = False) -> bool:
        stamp = item.updated_at if by_updated else item.created_at
        return (stamp, item.id) < (self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class ItemQuery:
    kinds: frozenset[Kind] = field(default_factory=frozenset)  # empty = any
    tags_all: frozenset[str] = field(default_factory=frozenset)
    statuses: frozenset[Status] = frozenset({Status.ACTIVE, Status.DRAFT})
    include_referenced: bool = True
    limit: int = 20
    after: Cursor | None = None
    by_updated: bool = False  # order by last change instead of creation

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE:
            raise ValidationError(f"limit must be between 1 and {MAX_PAGE}")
        if self.tags_all != normalize_tags(self.tags_all):
            raise ValidationError("tags must be normalised with normalize_tags()")

    def matches(self, item: Item, tier: Tier) -> bool:
        return (
            (not self.kinds or item.kind in self.kinds)
            and self.tags_all <= item.tags
            and item.status in self.statuses
            and (self.include_referenced or tier is Tier.PRIMARY)
            and (self.after is None or self.after.before(item, self.by_updated))
        )


@dataclass(frozen=True, slots=True)
class ItemView:
    """An item as one caller sees it; the tier lets answers say where knowledge came from."""

    item: Item
    tier: Tier


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[ItemView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Hit:
    """One index result (best chunk of one item); the repository re-checks access on hydrate."""

    item_id: str
    score: float
    chunk_ord: int = 0
