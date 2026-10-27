"""The tool set. Each tool resolves the caller from the verified token, calls one service method, shapes the result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.item import Item, ItemPatch, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.schemas import (
    ChangeOut,
    ConsultOut,
    ItemOut,
    LinkOut,
    LogOut,
    MatchOut,
    RememberOut,
)
from hive_mcp.security.principals import principal_from_claims
from hive_mcp.services.models import (
    ConsultRequest,
    ForeignPolicy,
    Match,
    RememberRequest,
    RememberResult,
    provenance,
)
from hive_mcp.services.sessions import SessionReport

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.access import Tier
    from hive_mcp.domain.principal import Principal

BODY_LIMIT = 20_000
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


def current_principal() -> Principal:
    """Identity comes only from the verified bearer token."""
    token = get_access_token()
    if token is None:
        raise PermissionDenied("authentication required")
    return principal_from_claims(token.subject, token.claims)


def scope_label(scope: Scope) -> str:
    return f"{scope.level.name.lower()}:{scope.owner}"


def _kinds(values: list[str] | None) -> frozenset[Kind]:
    try:
        return frozenset(Kind(v) for v in values or [])
    except ValueError as exc:
        raise ValidationError(
            f"unknown kind; use one of: {', '.join(k.value for k in Kind)}"
        ) from exc


def _kind(value: str) -> Kind:
    (kind,) = _kinds([value])
    return kind


def _match_out(m: Match) -> MatchOut:
    return MatchOut(
        id=m.item_id,
        title=m.title,
        snippet=m.snippet,
        kind=str(m.kind),
        tags=sorted(m.tags),
        status=str(m.status),
        score=round(m.score, 5),
        origin=m.provenance,
        external=m.tier.value == "referenced",
        scope=scope_label(m.scope),
        author=m.author_id,
        updated_at=m.updated_at.isoformat(),
        revision=m.revision,
        links=[
            LinkOut(
                relation=lk.relation,
                direction=lk.direction,
                id=lk.item_id,
                title=lk.title,
                external=lk.external,
            )
            for lk in m.links
        ],
    )


def _item_out(item: Item, tier: Tier) -> ItemOut:
    truncated = len(item.body) > BODY_LIMIT
    return ItemOut(
        id=item.id,
        title=item.title,
        body=item.body[:BODY_LIMIT],
        truncated=truncated,
        kind=str(item.kind),
        tags=sorted(item.tags),
        status=str(item.status),
        scope=scope_label(item.scope),
        visibility=str(item.visibility),
        author=item.author_id,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
        revision=item.revision,
        origin=provenance(item.scope, tier, item.author_id),
        external=tier.value == "referenced",
        has_log=bool(item.source and item.source.startswith("blob:")),
    )


def _change_out(item: Item) -> ChangeOut:
    return ChangeOut(
        id=item.id,
        revision=item.revision,
        scope=scope_label(item.scope),
        visibility=str(item.visibility),
        tags=sorted(item.tags),
        status=str(item.status),
    )


def _remember_out(r: RememberResult) -> RememberOut:
    notes = []
    if not r.created:
        notes.append("An identical earlier request was replayed; nothing new was stored.")
    if not r.indexed:
        notes.append("Stored. Search indexing is queued and will complete shortly.")
    if r.redactions:
        notes.append("Secrets were masked before storing: " + ", ".join(sorted(r.redactions)) + ".")
    return RememberOut(
        id=r.item.id,
        revision=r.item.revision,
        created=r.created,
        indexed=r.indexed,
        scope=scope_label(r.item.scope),
        redactions=r.redactions,
        message=" ".join(notes) or "Stored privately.",
    )


def register_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_consult(
        query: str,
        limit: int = 8,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
        other_groups: Literal["auto", "always", "never"] = "auto",
        include_links: bool = True,
        session: str | None = None,
    ) -> ConsultOut:
        """Search the shared knowledge base. Call this BEFORE starting a task, in plain words.

        Returns matches from your own scopes plus (when yours are thin) clearly labelled results from other groups.
        `kinds`: note, research, formula, measurement, decision, best_practice, doc, howto, session, compaction.
        `tags`: results must carry all of them (e.g. domain/rf). `other_groups`: auto, always or never.
        Results carry `links` (sources, newer versions, conflicts); read the notes: they flag results that
        another item supersedes or contradicts. Pass the same `session` label (any 4-80 character id of
        your choosing) to kb_consult, kb_get and kb_report_session to keep a reviewable trail of the
        notes this work relied on (see kb_session_sources).
        """
        if not 1 <= limit <= 25:  # noqa: PLR2004
            raise ValidationError("limit must be between 1 and 25")
        result = await get_services().consult.consult(
            current_principal(),
            ConsultRequest(
                query=query,
                limit=limit,
                kinds=_kinds(kinds),
                tags_all=normalize_tags(tags or []),
                include_foreign=ForeignPolicy(other_groups),
                with_links=include_links,
            ),
        )
        feedback = get_services().feedback
        if feedback:
            ids = [m.item_id for m in (*result.primary, *result.referenced)]
            await feedback.queried(current_principal(), query, ids)
        if session and (trail := get_services().session_sources):
            shown = [m.item_id for m in (*result.primary, *result.referenced)]
            await trail.record(current_principal(), session, shown=shown)
        return ConsultOut(
            results=[_match_out(m) for m in result.primary],
            from_other_groups=[_match_out(m) for m in result.referenced],
            notes=list(result.notes),
        )

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_get(id: str, session: str | None = None) -> ItemOut:
        """Read one item in full (use after kb_consult, or before kb_update to get its revision).

        Pass the `session` label you gave kb_consult to record that this work relied on the note."""
        view = await get_services().knowledge.get(current_principal(), id)
        if feedback := get_services().feedback:
            await feedback.opened(current_principal(), id)
        if session and (trail := get_services().session_sources):
            await trail.record(current_principal(), session, opened=[id])
        return _item_out(view.item, view.tier)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_remember(
        title: str,
        body: str,
        kind: str = "note",
        tags: list[str] | None = None,
        share_with_project: str | None = None,
        idempotency_key: str | None = None,
        source: str | None = None,
    ) -> RememberOut:
        """Store reusable knowledge (a finding, formula, measurement, decision, how-to, best practice).

        Private to the user unless `share_with_project` names a project they belong to. One idea per item.
        Use `idempotency_key` when retrying so the item is not stored twice. Do not include secrets.
        """
        scope = Scope(ScopeLevel.PROJECT, share_with_project) if share_with_project else None
        result = await get_services().knowledge.remember(
            current_principal(),
            RememberRequest(
                title=title,
                body=body,
                kind=next(iter(_kinds([kind])), Kind.NOTE),
                tags=tuple(tags or ()),
                scope=scope,
                idempotency_key=idempotency_key,
                source=source,
            ),
        )
        return _remember_out(result)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_update(
        id: str,
        expected_revision: int,
        title: str | None = None,
        body: str | None = None,
        kind: str | None = None,
        status: Literal["draft", "active", "superseded", "archived"] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> ChangeOut:
        """Edit an item you may write. Pass the revision you last read; a mismatch means someone else changed it.

        Use status=archived to retire an item, superseded when a newer item replaces it.
        """
        patch = ItemPatch(
            title=title,
            body=body,
            kind=_kind(kind) if kind else None,
            status=Status(status) if status else None,
            add_tags=normalize_tags(add_tags or []),
            remove_tags=normalize_tags(remove_tags or []),
        )
        item = await get_services().knowledge.update(
            current_principal(), id, patch, expected_revision
        )
        return _change_out(item)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_share(id: str, expected_revision: int, project: str) -> ChangeOut:
        """Share a private item with a project the user belongs to. Wider sharing needs an approver."""
        item = await get_services().knowledge.move(
            current_principal(),
            id,
            Scope(ScopeLevel.PROJECT, project),
            Visibility.SCOPE,
            expected_revision,
        )
        return _change_out(item)

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
