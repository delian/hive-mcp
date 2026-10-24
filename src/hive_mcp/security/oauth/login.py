"""Login policy and the three browser steps (start, upstream callback, consent), independent of HTTP."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.security.directory import normalize_email
from hive_mcp.security.oauth.upstream import GoogleIdentity, UpstreamError

if TYPE_CHECKING:
    from hive_mcp.security.directory import DirectoryUser, UserDirectory

log = logging.getLogger("hive.security")


@dataclass(frozen=True, slots=True)
class Denied:
    reason: str  # for logs only: the browser and the client only ever see "access_denied"


def check_identity(
    identity: GoogleIdentity, allowed_domains: list[str], directory: UserDirectory
) -> DirectoryUser | Denied:
    """Who may sign in: a verified Google Workspace account of an allowed domain that is in the directory."""
    if not identity.email_verified:
        return Denied("email not verified")
    try:
        email = normalize_email(identity.email)
    except Exception:
        return Denied("malformed email")
    domain = email.rpartition("@")[2]
    allowed = {d.strip().casefold() for d in allowed_domains}
    if domain not in allowed:
        return Denied(f"domain {domain} not allowed")
    # A Workspace account carries its domain as `hd`; a personal account using the same address does not.
    if (identity.hosted_domain or "").casefold() != domain:
        return Denied("not a Workspace account of that domain")
    user = directory.lookup(email)
    return user if user is not None else Denied("not in the user directory")


__all__ = ["Denied", "UpstreamError", "check_identity"]
