"""Best-effort secret redaction applied before anything is stored or shared.

This reduces accidents; it is no guarantee. Findings say what kind and how many, never the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # which group to replace (0 = the whole match)


def _rule(name: str, regex: str, group: int = 0, flags: int = 0) -> Rule:
    return Rule(name, re.compile(regex, flags), group)


RULES = (
    _rule(
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        0,
        re.S,
    ),
    _rule("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _rule("aws-secret", r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?([A-Za-z0-9/+=]{40})", 1),
    _rule("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    _rule("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    _rule("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _rule("llm-api-key", r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    _rule("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    _rule("bearer-token", r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})", 1),
    _rule("azure-account-key", r"(?i)AccountKey=([A-Za-z0-9+/=]{40,})", 1),
    _rule("url-password", r"://[^/\s:@]+:([^/\s@]{3,})@", 1),
    _rule(
        "credential-assignment",
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|pwd)\b[\"']?\s*[=:]\s*[\"']?([^\s\"',;]{8,})",
        1,
    ),
)


@dataclass(frozen=True, slots=True)
class Redaction:
    text: str
    findings: dict[str, int]  # rule name -> number of replacements

    @property
    def changed(self) -> bool:
        return bool(self.findings)


def redact(text: str) -> Redaction:
    findings: dict[str, int] = {}
    for rule in RULES:
        marker = f"[REDACTED:{rule.name}]"

        def replace(match: re.Match[str], rule: Rule = rule, marker: str = marker) -> str:
            if match.group(rule.group) == marker:  # already masked: keep the operation idempotent
                return match.group(0)
            findings[rule.name] = findings.get(rule.name, 0) + 1
            if rule.group == 0:
                return marker
            whole, start = match.group(0), match.start(rule.group) - match.start(0)
            return whole[:start] + marker + whole[start + len(match.group(rule.group)) :]

        text = rule.pattern.sub(replace, text)
    return Redaction(text, findings)
