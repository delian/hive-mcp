"""Session and compaction reports: what an agent records at the end of its work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.outputs import _remember_out
from hive_mcp.mcp_api.schemas import LogOut, RememberOut
from hive_mcp.mcp_api.tools import READ, WRITE, check_session_label, current_principal
from hive_mcp.services.sessions import SessionReport

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


def register_session_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    async def _report(  # one implementation for session and compaction reports
        kind: Literal["session", "compaction"],
        summary: str,
        log: str | None,
        outcomes: list[str] | None,
        project: str | None,
        idempotency_key: str | None,
    ) -> RememberOut:
        report = SessionReport(
            summary=summary,
            log=log,
            outcomes=tuple(outcomes or ()),
            project=project,
            idempotency_key=idempotency_key,
        )
        sessions = get_services().sessions
        send = sessions.report_session if kind == "session" else sessions.report_compaction
        return _remember_out(await send(current_principal(), report))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_report_session(
        summary: str,
        log: str | None = None,
        outcomes: list[str] | None = None,
        project: str | None = None,
        idempotency_key: str | None = None,
        session: str | None = None,
    ) -> RememberOut:
        """Record what happened in this work session: a short summary (searchable) and optionally the full log.

        Call at the end of a session. Stored privately. Secrets in the log are masked. With the
        `session` label used in kb_consult/kb_get, the stored note is linked `derived_from` the notes
        the work opened, so reviewers can see what it was built on (kb_graph).
        """
        check_session_label(session)
        out = await _report(
            "session",
            summary=summary,
            log=log,
            outcomes=outcomes,
            project=project,
            idempotency_key=idempotency_key,
        )
        if session and (trail := get_services().session_sources):
            await trail.link(current_principal(), session, out.id)
        return out

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_report_compaction(
        summary: str,
        log: str | None = None,
        outcomes: list[str] | None = None,
        project: str | None = None,
        idempotency_key: str | None = None,
    ) -> RememberOut:
        """Save the summary your context is being compacted into, so it can be recovered later."""
        return await _report(
            "compaction",
            summary=summary,
            log=log,
            outcomes=outcomes,
            project=project,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_get_log(id: str) -> LogOut:
        """Read the full stored log of a session report you can see."""
        text = await get_services().sessions.get_log(current_principal(), id)
        return LogOut(id=id, log=text)
