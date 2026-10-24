"""Tool outputs. Compact on purpose: agents pay for every token, so bodies are only returned by kb_get."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MatchOut(BaseModel):
    id: str
    title: str
    snippet: str
    kind: str
    tags: list[str]
    status: str
    score: float
    origin: str = Field(
        description="Where this knowledge lives. 'EXTERNAL: ...' means another group's."
    )
    external: bool = Field(description="True when it comes from a group you do not belong to.")
    scope: str
    author: str
    updated_at: str
    revision: int


class ConsultOut(BaseModel):
    results: list[MatchOut] = Field(
        description="From your own scopes (private notes, your projects, teams, org)."
    )
    from_other_groups: list[MatchOut] = Field(
        description="Shared by groups you do not belong to. Tell the user these are external; "
        "they may adopt, extend or ignore them."
    )
    notes: list[str]


class ItemOut(BaseModel):
    id: str
    title: str
    body: str
    truncated: bool
    kind: str
    tags: list[str]
    status: str
    scope: str
    visibility: str
    author: str
    created_at: str
    updated_at: str
    revision: int
    origin: str
    external: bool
    has_log: bool


class RememberOut(BaseModel):
    id: str
    revision: int
    created: bool = Field(description="False means an identical earlier call was replayed.")
    indexed: bool = Field(
        description="False means searchable text is queued and will appear shortly."
    )
    scope: str
    redactions: dict[str, int] = Field(
        description="Secrets that were masked before storing, by kind."
    )
    message: str


class ChangeOut(BaseModel):
    id: str
    revision: int
    scope: str
    visibility: str
    tags: list[str]
    status: str


class LogOut(BaseModel):
    id: str
    log: str
