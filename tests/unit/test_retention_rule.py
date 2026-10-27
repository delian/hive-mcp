import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.retention import MAX_DAYS, MIN_DAYS, RetentionRule, parse_rules
from hive_mcp.domain.scope import ScopeLevel


def test_days_are_limited_at_both_ends_inclusive():
    for days in (MIN_DAYS, MAX_DAYS):
        assert RetentionRule(days, Kind.SESSION).days == days
    for days in (MIN_DAYS - 1, MAX_DAYS + 1):
        with pytest.raises(ValidationError, match=f"{MIN_DAYS}-{MAX_DAYS}"):
            RetentionRule(days, Kind.SESSION)


def test_the_action_must_be_known_and_a_rule_needs_a_kind_or_a_scope():
    assert RetentionRule(5, Kind.NOTE, action="archive").action == "archive"
    with pytest.raises(ValidationError, match="action must be one of"):
        RetentionRule(5, Kind.NOTE, action="shred")
    with pytest.raises(
        ValidationError, match=r"^a retention rule needs a kind or a scope \(or both\)$"
    ):
        RetentionRule(5)
    assert RetentionRule(5, scope_level=ScopeLevel.PRIVATE).kind is None  # a scope alone is enough
    assert RetentionRule(5, scope_level=ScopeLevel.PRIVATE, kind=Kind.NOTE).days == 5


def test_a_rule_describes_itself():
    assert RetentionRule(90, Kind.SESSION).describe() == (
        "purge session in any scope, older than 90 days"
    )
    assert RetentionRule(7, scope_level=ScopeLevel.PRIVATE, action="archive").describe() == (
        "archive any kind in private, older than 7 days"
    )
    assert RetentionRule(7, Kind.NOTE, ScopeLevel.ORG).describe() == (
        "purge note in org, older than 7 days"
    )


def test_parsing_reads_each_field_and_defaults_to_purge():
    rules = parse_rules(
        [{"kind": "session", "days": "90"}, {"scope": "Private", "days": 3, "action": "archive"}]
    )
    assert rules == [
        RetentionRule(90, Kind.SESSION),
        RetentionRule(3, None, ScopeLevel.PRIVATE, "archive"),
    ]
    assert parse_rules([]) == []


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "session"},  # no days
        {"kind": "session", "days": 5, "colour": "red"},  # unknown key
        {"kind": "nonsense", "days": 5},
        {"scope": "galaxy", "days": 5},
        {"kind": "note", "days": "soon"},
        {"kind": "note", "days": None},
        "kind=note",
    ],
)
def test_parsing_refuses_malformed_entries_with_a_validation_error(entry):
    with pytest.raises(ValidationError, match="retention rule"):
        parse_rules([entry])
