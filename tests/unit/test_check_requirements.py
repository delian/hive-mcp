"""The rule: a change to the source must come with a change to the requirements, or say why not."""

from __future__ import annotations

from scripts.check_requirements import problem

CODE = "src/hive_mcp/services/knowledge.py"
REQ = "docs/REQUIREMENTS.md"
ANNEX = "docs/REQUIREMENTS-ANNEX.md"


def test_code_without_requirements_is_refused_and_names_the_files():
    reason = problem([CODE, "tests/unit/test_x.py"], ["Fix a thing"])
    assert reason and CODE in reason and "Requirements: none" in reason


def test_either_requirements_file_satisfies_the_rule():
    assert problem([CODE, REQ], ["m"]) is None
    assert problem([CODE, ANNEX], ["m"]) is None


def test_changes_outside_the_source_need_nothing():
    assert (
        problem(["tests/unit/test_x.py", "docs/USAGE.md", "uv.lock", "scripts/x.py"], ["m"]) is None
    )
    assert problem(["src/hive_mcp/py.typed", "src/hive_mcp/data.json"], ["m"]) is None


def test_the_client_package_counts_as_source():
    assert problem(["clients/hooks/src/hive_hooks/cli.py"], ["m"])


def test_a_waiver_line_in_any_commit_message_of_the_range_is_accepted():
    assert problem([CODE], ["Refactor\n\nRequirements: none\n\nCo-Authored-By: x", "other"]) is None
    assert problem([CODE], ["requirements: NONE"]) is None  # case does not matter


def test_the_waiver_must_be_its_own_line():
    assert problem([CODE], ["I wrote Requirements: none somewhere in a sentence"])
    assert problem([CODE], ["Requirements: none, mostly"])


def test_many_files_are_summarised():
    reason = problem([f"src/hive_mcp/m{i}.py" for i in range(9)], ["m"])
    assert reason and "and 4 more" in reason
