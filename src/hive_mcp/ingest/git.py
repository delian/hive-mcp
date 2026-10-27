"""Ingesting a Git repository: shallow clone, then pick the files worth reading.

Operator command only (`hive ingest-git`), never an agent tool: cloning runs `git` against an
address supplied by whoever types the command. Nothing from the repository is executed.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path, PurePosixPath

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.extract import HTML_SUFFIXES, TEXT_SUFFIXES
from hive_mcp.ingest.files import files_under
from hive_mcp.ingest.office import OFFICE

READABLE = TEXT_SUFFIXES | HTML_SUFFIXES | frozenset(OFFICE) | {".pdf"}
DEFAULT_EXCLUDES = (
    "node_modules/*", "vendor/*", "dist/*", "build/*", "target/*", "__pycache__/*", "*.lock",
    "*.min.js", "*.min.css", "package-lock.json", "*.svg", "*.map",
)  # fmt: skip
MAX_FILE_BYTES = 1024 * 1024
CLONE_TIMEOUT = 300


def repo_name(url: str) -> str:
    last = PurePosixPath(url.rstrip("/").removesuffix(".git").replace(":", "/")).name
    return last or "repo"


def clone(url: str, destination: Path, ref: str | None = None) -> None:
    if url.startswith("-") or "\n" in url or not url.strip():
        raise ValidationError("not a usable repository address")
    command = ["git", "-c", "protocol.ext.allow=never", "clone", "--depth", "1", "--quiet",
               "--no-tags", "--single-branch", "--no-recurse-submodules"]  # fmt: skip
    if ref:
        command += ["--branch", ref]
    command += ["--", url, str(destination)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never wait for a password
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; the address follows "--"
            command, capture_output=True, text=True, check=False, timeout=CLONE_TIMEOUT, env=env
        )
    except FileNotFoundError as exc:
        raise ValidationError("git is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"cloning took longer than {CLONE_TIMEOUT} seconds") from exc
    if result.returncode:
        raise ValidationError(
            f"git clone failed: {result.stderr.strip().splitlines()[-1:] or ['?']}"
        )


def _excluded(relative: str, name: str, patterns: tuple[str, ...]) -> bool:
    """A pattern such as `vendor/*` names a directory: it applies at any depth."""
    folders = relative.split("/")[:-1]
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        if pattern.endswith("/*") and any(fnmatch.fnmatch(f, pattern[:-2]) for f in folders):
            return True
    return False


def select_files(
    root: Path, excludes: tuple[str, ...] = DEFAULT_EXCLUDES, max_bytes: int = MAX_FILE_BYTES
) -> tuple[list[Path], int]:
    """(files to ingest, how many were skipped as unreadable, excluded or too big)."""
    chosen, skipped = [], 0
    for path in files_under([root]):
        relative = path.relative_to(root).as_posix()
        readable = path.suffix.lower() in READABLE or path.name in {
            "Makefile",
            "Dockerfile",
            "LICENSE",
        }
        if (
            not readable
            or _excluded(relative, path.name, excludes)
            or path.stat().st_size > max_bytes
        ):
            skipped += 1
        else:
            chosen.append(path)
    return chosen, skipped
