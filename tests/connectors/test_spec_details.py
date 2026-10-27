"""Connector settings: each rule and its message, exactly."""

from __future__ import annotations

import pytest

from hive_mcp.connectors.spec import DEFAULT_MINUTES, MIN_MINUTES, REQUIRED, TYPES, parse_specs
from hive_mcp.domain.errors import ValidationError

BASE = {"name": "docs", "org": "acme", "owner": "bot", "project": "p1"}


def refused(entry) -> str:
    with pytest.raises(ValidationError) as caught:
        parse_specs([entry])
    return str(caught.value)


def test_a_valid_entry_is_read_with_its_defaults_and_options():
    [spec] = parse_specs(
        [{**BASE, "type": "urls", "urls": ["https://a.example"], "tags": ["B/x", "a/y"]}]
    )
    assert (spec.name, spec.type, spec.org, spec.owner, spec.project) == (
        "docs", "urls", "acme", "bot", "p1",
    )  # fmt: skip
    assert spec.every_minutes == DEFAULT_MINUTES and spec.options == {"urls": ["https://a.example"]}
    assert spec.tags == ("a/y", "b/x")  # normalised and sorted
    [slow] = parse_specs([{**BASE, "type": "urls", "urls": ["u"], "every_minutes": "90"}])
    assert slow.every_minutes == 90
    [edge] = parse_specs([{**BASE, "type": "urls", "urls": ["u"], "every_minutes": MIN_MINUTES}])
    assert edge.every_minutes == MIN_MINUTES
    assert parse_specs([]) == []


def test_the_type_must_be_known_and_the_message_lists_them():
    for entry in ({"name": "x"}, {**BASE, "type": "ftp"}, "not a dict", None):
        message = refused(entry)
        assert message.endswith(f"type must be one of {sorted(TYPES)}") and "connector" in message


def test_names_are_lowercase_and_unique_and_unknown_options_are_named():
    assert "a lowercase name is required" in refused(
        {**BASE, "type": "urls", "urls": ["u"], "name": ""}
    )
    assert refused({**BASE, "type": "urls", "urls": ["u"], "name": "Docs"}).startswith(
        "connector 'Docs':"
    )
    assert "a lowercase name is required" in refused(
        {**BASE, "type": "urls", "urls": ["u"], "name": "-x"}
    )
    long = "a" * 41
    assert "lowercase name" in refused({**BASE, "type": "urls", "urls": ["u"], "name": long})
    odd = refused({**BASE, "type": "urls", "urls": ["u"], "repo": "x/y"})
    assert (
        odd == "connector 'docs': a lowercase name is required and ['repo'] are not options of urls"
    )
    nameless = refused({"type": "urls", "urls": ["u"], "org": "a", "owner": "b", "project": "c"})
    assert nameless.startswith("connector {") and "a lowercase name is required" in nameless
    with pytest.raises(ValidationError) as twice:
        parse_specs(
            [{**BASE, "type": "urls", "urls": ["u"]}, {**BASE, "type": "urls", "urls": ["u"]}]
        )
    assert str(twice.value) == "connector name 'docs' is used twice"


@pytest.mark.parametrize("missing", ["org", "owner", "project"])
def test_org_owner_and_project_are_required_and_blank_does_not_count(missing):
    entry = {**BASE, "type": "urls", "urls": ["u"]}
    del entry[missing]
    assert refused(entry) == f"connector docs: {missing} is required"
    assert refused({**entry, missing: "   "}) == f"connector docs: {missing} is required"


def test_the_interval_has_a_floor_and_the_message_says_it():
    entry = {**BASE, "type": "urls", "urls": ["u"], "every_minutes": MIN_MINUTES - 1}
    assert refused(entry) == f"connector docs: every_minutes must be at least {MIN_MINUTES}"


@pytest.mark.parametrize("kind", sorted(REQUIRED))
def test_every_type_names_its_missing_required_option(kind):
    full = {
        "urls": {"urls": ["u"]}, "github": {"repo": "o/r"},
        "drive": {"folder": "f", "key_file": "k", "as_user": "u@x.com"},
        "confluence": {"base_url": "https://c", "space": "S", "email": "e@x.com", "token_env": "T"},
        "jira": {"base_url": "https://j", "jql": "x", "email": "e@x.com", "token_env": "T"},
        "notion": {"token_env": "T"},
        "sharepoint": {"tenant": "t", "client_id": "c", "secret_env": "S", "drive_id": "d"},
    }[kind]  # fmt: skip
    assert parse_specs([{**BASE, "type": kind, **full}])[0].options == full
    for key in REQUIRED[kind]:
        partial = {k: v for k, v in full.items() if k != key}
        assert refused({**BASE, "type": kind, **partial}) == (
            f"connector docs: {key} is required for {kind}"
        )
        assert refused({**BASE, "type": kind, **partial, key: ""}) == (
            f"connector docs: {key} is required for {kind}"
        )


def test_urls_and_paths_must_be_lists_of_the_right_things():
    assert refused({**BASE, "type": "urls", "urls": "https://a"}) == (
        "connector docs: urls must be a list of addresses"
    )
    assert refused({**BASE, "type": "urls", "urls": ["ok", 5]}) == (
        "connector docs: urls must be a list of addresses"
    )
    assert refused({**BASE, "type": "github", "repo": "o/r", "paths": "docs/"}) == (
        "connector docs: paths must be a list"
    )
    [spec] = parse_specs([{**BASE, "type": "github", "repo": "o/r", "paths": ["docs/"]}])
    assert spec.options["paths"] == ["docs/"]
    [no_paths] = parse_specs([{**BASE, "type": "github", "repo": "o/r"}])
    assert "paths" not in no_paths.options


def test_a_bad_name_alone_does_not_mention_options():
    entry = {**BASE, "type": "urls", "urls": ["u"], "name": "Docs"}
    assert refused(entry) == "connector 'Docs': a lowercase name is required"
