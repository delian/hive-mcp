"""Maps domain errors to tool errors an agent can act on, and hides everything else."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver.exceptions import ToolError

from hive_mcp.domain.errors import Conflict, HiveError, NotFound, StorageBusy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


def user_message(exc: HiveError) -> str:
    if isinstance(exc, NotFound):
        return "not found, or not visible to you"  # never reveal which
    if isinstance(exc, Conflict):
        return f"{exc}. Re-read the item with kb_get and retry with its current revision."
    if isinstance(exc, StorageBusy):
        return "temporarily unavailable; retry in a few seconds"
    return str(exc) or exc.__class__.__name__


def guarded[T](fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Expected failures become readable tool errors; unexpected ones are logged and reported opaquely."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:  # noqa: ANN401 - transparent wrapper
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except HiveError as exc:
            raise ToolError(user_message(exc)) from exc
        except Exception:
            log.exception("unexpected error in tool %s", fn.__name__)
            raise ToolError("internal error; the operation was not completed") from None

    return wrapper
