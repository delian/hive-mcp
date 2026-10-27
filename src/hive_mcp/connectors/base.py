"""What a connector produces and what a sync reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


@dataclass(frozen=True, slots=True)
class Document:
    key: str  # stable identity in the source (a URL, a repository path, a Drive file id)
    label: str  # shown in reports
    # (file name whose extension tells the extractor what the bytes are, content); called only
    # when the document may have changed
    load: Callable[[], Awaitable[tuple[str, bytes]]]
    version: str | None = None  # the source's own change marker (blob sha, modified time)
    tags: tuple[str, ...] = ()


class Connector(Protocol):
    def documents(self) -> AsyncIterator[Document]:
        """Every document that should be in the knowledge base now. Raises when the listing
        could not be completed: a partial listing must never retire documents."""


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    retired: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # document key -> reason
    aborted: str | None = None  # why nothing was retired or the run stopped early

    def line(self) -> str:
        text = (
            f"{self.created} new, {self.updated} updated, {self.unchanged} unchanged, "
            f"{self.retired} retired, {len(self.failed)} failed"
        )
        return f"{text}; {self.aborted}" if self.aborted else text
