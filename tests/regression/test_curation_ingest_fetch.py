"""Regression tests for fetching documents from URLs (BUG-CUR-n)."""

from __future__ import annotations

import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ingest.extract import extract_text
from hive_mcp.ingest.fetch import FetchPolicy, _check_url, _file_name, is_public


def test_a_page_served_as_html_is_parsed_as_html_whatever_its_url_ends_with():
    """BUG-CUR-11: /wiki/Node.js was named *.js and stored as raw HTML source; /blog/v1.2 was
    refused as 'unsupported file type .2'."""
    for url, mime in (
        ("https://en.wikipedia.org/wiki/Node.js", "text/html; charset=utf-8"),
        ("https://example.org/blog/v1.2", "text/html"),
    ):
        name = _file_name(url, mime)
        assert name.endswith(".html")
        page = b"<html><body><p>Hello <b>there</b></p></body></html>"
        assert extract_text(name, page).text == "Hello there"
    assert _file_name("https://example.org/notes.md", "text/plain").endswith("notes.md")
    assert _file_name("https://example.org/a.pdf", "application/octet-stream").endswith("a.pdf")


@pytest.mark.parametrize("url", ["http://a:99999/", "http://[bad/", "http://a:xx/x"])
def test_malformed_urls_are_a_validation_error_not_a_crash(url):
    """BUG-CUR-12: a bad port or bracket raised a bare ValueError from urlsplit/.port."""
    with pytest.raises(ValidationError):
        _check_url(url, FetchPolicy())


def test_ipv4_compatible_and_site_local_ipv6_addresses_are_not_public():
    """BUG-CUR-13: ::10.0.0.1 and fec0::/10 passed the public-address check (SSRF)."""
    assert not is_public("::10.0.0.1") and not is_public("::7f00:1") and not is_public("fec0::1")
    assert is_public("8.8.8.8") and is_public("2606:4700::1111")


def test_nested_vendor_and_node_modules_folders_are_skipped(tmp_path):
    """BUG-CUR-14: `node_modules/*` only matched at the repository root, so the dependencies of a
    monorepo package (packages/web/node_modules/...) were ingested."""
    from hive_mcp.ingest.git import select_files

    for rel in (
        "README.md",
        "docs/guide.md",
        "node_modules/a/readme.md",
        "packages/web/node_modules/left-pad/README.md",
        "services/api/vendor/lib/doc.md",
        "web/dist/bundle.txt",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("some text\n")
    chosen, skipped = select_files(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in chosen) == [
        "README.md",
        "docs/guide.md",
    ]
    assert skipped == 4
