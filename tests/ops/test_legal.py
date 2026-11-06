"""The built-in privacy and terms pages, and the settings that replace them."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hive_mcp.config import Settings
from hive_mcp.legal import legal_routes


def client(**settings) -> TestClient:
    return TestClient(Starlette(routes=legal_routes(Settings(**settings))), follow_redirects=False)


def test_the_pages_name_the_operator_and_the_contact_and_escape_both():
    c = client(legal_org="Acme <Labs>", legal_contact='ops@acme.com"><script>')
    privacy, terms = c.get("/legal/privacy"), c.get("/legal/terms")
    for page in (privacy, terms):
        assert page.status_code == 200 and "Acme &lt;Labs&gt;" in page.text
        assert (
            "<script>" not in page.text
            and "mailto:ops@acme.com&quot;&gt;&lt;script&gt;" in page.text
        )
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
    assert "What is stored" in privacy.text and "Teams" in privacy.text
    assert "/legal/privacy" in terms.text and "Terms of use" in terms.text


def test_without_a_contact_the_page_says_to_ask_the_operators():
    page = client().get("/legal/privacy")
    assert "mailto:" not in page.text and "ask the people who run this server" in page.text
    assert "the operator of this Hive server" in page.text


def test_your_own_documents_replace_the_built_in_pages():
    c = client(privacy_url="https://acme.com/privacy", terms_url="https://acme.com/terms")
    for path, target in (
        ("/legal/privacy", "https://acme.com/privacy"),
        ("/legal/terms", "https://acme.com/terms"),
    ):
        reply = c.get(path)
        assert reply.status_code == 302 and reply.headers["location"] == target
    mixed = client(privacy_url="https://acme.com/privacy")
    assert mixed.get("/legal/terms").status_code == 200


def test_your_own_markdown_file_replaces_the_text_and_is_escaped(tmp_path):
    f = tmp_path / "privacy.md"
    f.write_text(
        "# Our privacy\nWe keep <b>notes</b> for **90 days**.\n\n- one\n- two\n"
        "See [policy](https://acme.com/p) and [bad](javascript:alert(1)).\n",
        encoding="utf-8",
    )
    page = client(privacy_file=f).get("/legal/privacy")
    assert page.status_code == 200
    assert "<h2>Our privacy</h2>" in page.text and "<strong>90 days</strong>" in page.text
    assert "&lt;b&gt;notes&lt;/b&gt;" in page.text and "<b>notes" not in page.text
    assert "<ul><li>one</li><li>two</li></ul>" in page.text
    assert "<a href='https://acme.com/p'>policy</a>" in page.text
    assert (
        "javascript:" not in page.text.split("[bad]")[0] or "<a href='javascript" not in page.text
    )
    assert "What is stored" not in page.text
    f.write_text("changed", encoding="utf-8")  # read on every request
    assert "changed" in client(privacy_file=f).get("/legal/privacy").text


def test_a_url_beats_a_file_and_a_file_beats_the_built_in_text(tmp_path):
    f = tmp_path / "t.md"
    f.write_text("our terms", encoding="utf-8")
    both = client(terms_file=f, terms_url="https://acme.com/terms")
    assert both.get("/legal/terms").headers["location"] == "https://acme.com/terms"
    assert "our terms" in client(terms_file=f).get("/legal/terms").text
    assert "What is stored" in client(terms_file=f).get("/legal/privacy").text


def test_a_missing_file_stops_startup_with_the_setting_name(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="HIVE_TERMS_FILE"):
        client(terms_file=tmp_path / "nope.md")
