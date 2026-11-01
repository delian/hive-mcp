"""Python, dependencies and tools are managed with uv alone, and the image knows every extra."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
# Written about, not used: history and the rules that describe this very check.
EXEMPT = {
    "docs/PLAN.md",
    "docs/BENCHMARK.md",
    "docs/REQUIREMENTS.md",
    "docs/REQUIREMENTS-ANNEX.md",
    "CLAUDE.md",
    "tests/unit/test_uv_only.py",
}
FORBIDDEN_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|constraints[^/]*\.txt|Pipfile(\.lock)?|setup\.(py|cfg)|"
    r"poetry\.lock|environment\.ya?ml)$"
)
FORBIDDEN_TEXT = re.compile(r"(?<!uv )\bpip3? install\b|\bpipx\b|python3? -m venv|\bvirtualenv\b")
# Extras that the image leaves out on purpose (each costs size; build with --build-arg EXTRAS).
NOT_IN_IMAGE = {"lancedb", "parquet"}


GIT = shutil.which("git")


def tracked() -> list[str]:
    if GIT is None:
        pytest.skip("git is not installed")
    try:
        out = subprocess.run(  # noqa: S603
            [GIT, "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    return out.split()


def test_the_project_is_defined_by_pyproject_and_uv_lock_alone():
    files = tracked()
    assert not [f for f in files if FORBIDDEN_FILES.search(f)]
    assert "uv.lock" in files and ".python-version" in files and "pyproject.toml" in files


def test_nothing_installs_with_pip_pipx_or_a_hand_made_venv():
    offenders = []
    for name in tracked():
        path = ROOT / name
        if name in EXEMPT or path.suffix in {".lock", ".png", ".ico", ".svg"} or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        offenders += [f"{name}: {m.group(0)}" for m in FORBIDDEN_TEXT.finditer(text)]
    assert not offenders, offenders


def test_every_optional_extra_is_in_the_image_or_deliberately_left_out():
    extras = set(
        tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    )
    dockerfile = (ROOT / "Dockerfile").read_text()
    default = re.search(r'ARG EXTRAS="([^"]*)"', dockerfile)
    assert default, "the Dockerfile must declare its default EXTRAS"
    in_image = set(re.findall(r"--extra ([a-z-]+)", default.group(1)))
    assert extras == in_image | NOT_IN_IMAGE, (
        f"extras {sorted(extras)}: image has {sorted(in_image)}, left out on purpose "
        f"{sorted(NOT_IN_IMAGE)}; add the new one to the Dockerfile or to NOT_IN_IMAGE"
    )
    assert not (in_image & NOT_IN_IMAGE)
