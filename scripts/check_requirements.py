"""Fail when source code changed but the requirements documents did not.

docs/REQUIREMENTS.md and docs/REQUIREMENTS-ANNEX.md must describe the code well enough to
rebuild it, so a change under `src/` or `clients/hooks/src/` has to touch one of them. A change
that really alters no requirement (a refactor, a pure bug fix to documented behaviour) says so
with a line `Requirements: none` in a commit message of the range.

Usage: python scripts/check_requirements.py [base-commit]   (default: the parent of HEAD)
"""

from __future__ import annotations

import shutil
import subprocess
import sys

SOURCE = ("src/", "clients/hooks/src/")
REQUIREMENTS = ("docs/REQUIREMENTS.md", "docs/REQUIREMENTS-ANNEX.md")
WAIVER = "requirements: none"
NO_COMMIT = "0" * 40
SHOWN = 5


def problem(changed: list[str], messages: list[str]) -> str | None:
    """The reason the change is not acceptable, or None."""
    code = [f for f in changed if f.startswith(SOURCE) and f.endswith(".py")]
    if not code or any(f in REQUIREMENTS for f in changed):
        return None
    if any(line.strip().lower() == WAIVER for m in messages for line in m.splitlines()):
        return None
    shown = ", ".join(code[:SHOWN]) + (
        f" and {len(code) - SHOWN} more" if len(code) > SHOWN else ""
    )
    return (
        f"source changed ({shown}) but neither {REQUIREMENTS[0]} nor {REQUIREMENTS[1]} did.\n"
        "Update the requirements in the same change, or add a line `Requirements: none` to a "
        "commit message if no requirement is affected."
    )


GIT = shutil.which("git") or "git"


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, *args], capture_output=True, text=True, check=True
    ).stdout


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else ""
    if not base or base == NO_COMMIT:
        base = "HEAD~1"
    changed = git("diff", "--name-only", f"{base}..HEAD").split()
    messages = git("log", "--format=%B%x00", f"{base}..HEAD").split("\x00")
    reason = problem(changed, messages)
    if reason:
        print(reason, file=sys.stderr)
        return 1
    print("requirements check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
