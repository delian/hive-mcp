"""What every write goes through: hidden characters removed, secrets masked, injection flagged."""

from __future__ import annotations

from dataclasses import dataclass

from hive_mcp.security.injection import scan, strip_invisible
from hive_mcp.security.redact import redact


@dataclass(frozen=True, slots=True)
class Sanitized:
    title: str
    body: str
    findings: dict[str, int]  # rule name -> count; "injection:<heuristic>" for flags
    flagged: bool  # the text looks like an attack on the agents that will read it


def _clean(text: str, findings: dict[str, int]) -> str:
    text, hidden = strip_invisible(text)
    if hidden:
        findings["invisible-characters"] = findings.get("invisible-characters", 0) + hidden
    masked = redact(text)
    for name, n in masked.findings.items():
        findings[name] = findings.get(name, 0) + n
    return masked.text


def sanitize(title: str, body: str) -> Sanitized:
    findings: dict[str, int] = {}
    clean_title, clean_body = _clean(title, findings), _clean(body, findings)
    flags = scan(f"{clean_title}\n{clean_body}")
    findings.update({f"injection:{name}": 1 for name in flags})
    return Sanitized(clean_title, clean_body, findings, bool(flags))
