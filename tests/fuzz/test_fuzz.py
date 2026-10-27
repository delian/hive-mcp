"""Property and fuzz tests for the code that reads untrusted text, files and JSON.

The contract is the same everywhere: whatever arrives, the code returns something sensible or
raises the project's own validation error, never another exception, and never takes unreasonably
long (regular expressions are the usual way to hang a server).
"""

from __future__ import annotations

import contextlib
import json
import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hive_mcp.connectors.atlassian import adf_text
from hive_mcp.connectors.notion import page_title, rich_text
from hive_mcp.connectors.spec import parse_specs
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.retention import parse_rules
from hive_mcp.ingest.extract import extract_text
from hive_mcp.integrations.config import IntegrationConfig
from hive_mcp.llm.providers import LlmError
from hive_mcp.security.injection import scan, strip_invisible
from hive_mcp.security.sanitize import sanitize
from hive_mcp.services.assist import _json
from hive_mcp.slack.routes import wanted_mention
from hive_mcp.slack.verify import verify
from hive_mcp.teams.handlers import clean_text
from hive_mcp.webui.ops_pages import parse_mapping

FUZZ = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
TEXT = st.text(max_size=400)
JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-5, 5)
    | st.floats(allow_nan=False)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=25,
)


def quickly(fn, *args, limit: float = 2.0):
    started = time.perf_counter()
    result = fn(*args)
    assert time.perf_counter() - started < limit, f"{fn.__name__} took too long"
    return result


# ---------------------------------------------------------------- text
@FUZZ
@given(TEXT, TEXT)
def test_sanitising_never_fails_and_is_stable(title, body):
    once = sanitize(title, body)
    twice = sanitize(once.title, once.body)
    assert (twice.title, twice.body) == (once.title, once.body)  # nothing more to remove or mask
    assert "\u200b" not in once.body and "﻿" not in once.title


@FUZZ
@given(TEXT)
def test_invisible_characters_are_gone_whatever_the_input(text):
    clean, count = strip_invisible(text)
    assert strip_invisible(clean) == (clean, 0) and count >= 0 and len(clean) <= len(text)


ADVERSARIAL = [
    "ignore " * 5000, "ignore all previous " * 2000, "send " + "the secrets " * 3000 + "to ",
    "![" * 5000, "![a](http://x?" + "q=" * 5000, "do not " * 5000, "system:" + " you" * 5000,
    "<" * 5000, "AKIA" * 3000, "password=" * 3000, "-----BEGIN PRIVATE KEY-----" * 2000,
    "a" * 200_000, "\n" * 100_000, " " * 100_000, "\t\n" * 50_000, "system:\n" * 20_000, "=" * 100_000, "ıgnore " * 4000,
]  # fmt: skip


@pytest.mark.parametrize("text", ADVERSARIAL, ids=range(len(ADVERSARIAL)))
def test_no_input_makes_the_text_checks_slow(text):
    quickly(scan, text)
    quickly(sanitize, text[:50_000], text[:50_000], limit=5.0)


@FUZZ
@given(TEXT)
def test_chat_text_cleaning_never_fails(text):
    assert isinstance(clean_text(text), str)


@FUZZ
@given(TEXT)
def test_mapping_forms_are_a_dict_or_a_validation_error(text):
    try:
        found = parse_mapping(text)
    except HiveError:
        return
    assert all(k and v for k, v in found.items())


# ---------------------------------------------------------------- settings and payloads
@FUZZ
@given(st.lists(JSON, max_size=4))
def test_connector_and_retention_settings_fail_only_with_validation_errors(raw):
    for parse in (parse_specs, parse_rules):
        with contextlib.suppress(HiveError):
            parse(raw)


@FUZZ
@given(st.dictionaries(st.text(max_size=10), JSON, max_size=6))
def test_a_chat_event_of_any_shape_is_ignored_or_answered_never_a_crash(payload):
    config = IntegrationConfig(
        "slack", True, "T1", allow_mentions=True, secrets={"signing_secret": "x"}
    )
    wanted_mention(config, payload)
    wanted_mention(config, {"type": "event_callback", "team_id": "T1", "event": payload})


@FUZZ
@given(JSON)
def test_flattening_foreign_documents_never_fails(doc):
    assert isinstance(adf_text(doc), str)
    assert isinstance(rich_text(doc), str)
    page_title(doc if isinstance(doc, dict) else {})


@FUZZ
@given(TEXT)
def test_model_replies_become_a_dict_or_an_llm_error(reply):
    with contextlib.suppress(LlmError):
        assert isinstance(_json(reply), dict)


@FUZZ
@given(st.none() | st.text(max_size=30), st.none() | st.text(max_size=80), st.binary(max_size=200))
def test_slack_signature_checks_reject_garbage_without_raising(timestamp, signature, body):
    assert verify("secret", timestamp, signature, body) is False


@FUZZ
@given(st.dictionaries(st.text(max_size=10), JSON, max_size=8))
def test_integration_settings_validation_raises_only_validation_errors(fields):
    config = IntegrationConfig(
        "teams",
        enabled=bool(fields.get("enabled")),
        workspace=str(fields.get("workspace", "")),
        app_id=str(fields.get("app_id", "")),
        channel_projects={
            str(k): str(v)
            for k, v in (fields.get("c", {}) if isinstance(fields.get("c"), dict) else {}).items()
        },
    )
    with contextlib.suppress(HiveError):
        config.validate()


# ---------------------------------------------------------------- files from strangers
SUFFIXES = [
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".html",
    ".htm",
    ".csv",
    ".json",
    ".md",
    ".txt",
    ".xml",
    ".png",
]


@pytest.mark.parametrize("suffix", SUFFIXES)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.binary(max_size=3000))
def test_random_bytes_in_any_supported_format_give_text_or_a_validation_error(suffix, data):
    try:
        out = quickly(extract_text, f"file{suffix}", data, None, limit=10.0)
    except HiveError:
        return
    assert isinstance(out.text, str)


def zip_with(names_and_bytes: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in names_and_bytes.items():
            z.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("suffix", "members"),
    [
        (".docx", {}), (".docx", {"word/document.xml": b"<not xml"}),
        (".docx", {"word/document.xml": b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>"}),
        (".xlsx", {"xl/workbook.xml": b"<x/>"}), (".xlsx", {"xl/sharedStrings.xml": b"\xff\xfe"}),
        (".pptx", {"ppt/slides/slide1.xml": b"<a:p xmlns:a='x'><a:t>hi</a:t></a:p>"}),
        (".pptx", {"ppt/slides/slide1.xml": b"<<<"}),
    ],
)  # fmt: skip
def test_malformed_office_containers_are_validation_errors_not_crashes(suffix, members):
    with contextlib.suppress(HiveError):
        extract_text(f"f{suffix}", zip_with(members), None)


def test_a_zip_bomb_is_refused_quickly():
    bomb = zip_with({"word/document.xml": b"<w:t>" + b"a" * 100_000_000 + b"</w:t>"})
    started = time.perf_counter()
    with contextlib.suppress(HiveError):
        extract_text("bomb.docx", bomb, None)
    assert time.perf_counter() - started < 20


def test_deeply_nested_json_and_html_do_not_blow_the_stack():
    nested = ("[" * 100_000 + "]" * 100_000).encode()
    for name in ("deep.json", "deep.html", "deep.xml"):
        with contextlib.suppress(HiveError):
            extract_text(name, nested if name.endswith("json") else b"<div>" * 100_000, None)
    assert json.dumps({})  # reached: no RecursionError escaped


@FUZZ
@given(TEXT)
def test_card_text_escaping_never_fails_and_leaves_no_bare_link_syntax(text):
    from hive_mcp.teams.extension import plain

    out = plain(text)
    assert "](" not in out.replace("\\](", "") and "<" not in out.replace("\\<", "")


@FUZZ
@given(st.dictionaries(st.text(max_size=12), JSON, max_size=8))
def test_an_invoke_of_any_shape_gets_a_body_never_a_crash(activity):
    import asyncio

    from hive_mcp.teams.extension import TeamsExtension

    class Nobody:
        async def resolve(self, _activity):
            return None

    for name in ("composeExtension/query", "composeExtension/fetchTask", "other", None):
        body = asyncio.run(
            TeamsExtension(Nobody(), "t", {}).invoke({**activity, "name": name})  # type: ignore[arg-type]
        )
        assert isinstance(body, dict)
