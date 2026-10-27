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
