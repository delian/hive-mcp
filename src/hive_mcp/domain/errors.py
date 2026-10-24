"""Domain errors. Adapters raise these; the MCP layer maps them to tool errors."""

from __future__ import annotations


class HiveError(Exception):
    """Base class for all expected, user-visible errors."""


class ValidationError(HiveError):
    """Input is malformed or violates an invariant."""


class NotFound(HiveError):
    """Item does not exist *or is not visible* to the caller (never distinguish the two)."""


class PermissionDenied(HiveError):
    """Caller can see the item but may not perform this write."""


class Conflict(HiveError):
    """Optimistic-concurrency failure: the item changed since the caller read it."""


class IdempotencyConflict(HiveError):
    """An idempotency key was reused with a different payload."""


class StorageBusy(HiveError):
    """The database is locked, unreachable or overloaded; the caller may retry."""


class EmbeddingUnavailable(HiveError):
    """The embedding provider failed or is unreachable; callers degrade to keyword search."""
