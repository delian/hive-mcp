"""The one tokenizer: Unicode-aware, case-insensitive (fixes ASCII-only matching of naive LIKE)."""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


# Words so common that matching on them finds noise ("the", "for"). Only the query side drops them:
# documents keep every word, and a query made only of such words still searches.
STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "may",
        "more",
        "most",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


MAX_QUERY_TERMS = 64


def query_terms(query: str) -> list[str]:
    """A query's words minus stopwords (or all of them if only stopwords are left)."""
    words = tokenize(query)  # a pasted page is not a query
    return ([w for w in words if w not in STOPWORDS] or words)[:MAX_QUERY_TERMS]
