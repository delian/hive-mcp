"""Helpers shared by the operator command modules."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import typer

from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


def run_cli(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    try:
        asyncio.run(coro_fn())
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
