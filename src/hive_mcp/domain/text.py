"""Text that no store can hold: NUL characters and lone UTF-16 surrogates.

JSON can carry a lone surrogate (`"\\ud800"`) and Python decodes it into a `str` that cannot be
encoded as UTF-8, so a database driver (or a hash) fails with a `UnicodeEncodeError` deep inside a
write. Rejecting such text at the domain boundary turns that into an ordinary `ValidationError`.
"""

from __future__ import annotations

import re

from hive_mcp.domain.errors import ValidationError

_BAD = re.compile("[\x00\ud800-\udfff]")


def has_bad_text(*texts: str) -> bool:
    return any(_BAD.search(t) for t in texts)


def check_text(*texts: str, what: str = "text") -> None:
    if has_bad_text(*texts):
        raise ValidationError(f"{what} must not contain NUL characters or lone surrogates")
