"""The one tokenizer: Unicode-aware, case-insensitive (fixes ASCII-only matching of naive LIKE)."""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())
