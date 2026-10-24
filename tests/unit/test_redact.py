import pytest
from hypothesis import given
from hypothesis import strategies as st

from hive_mcp.security.redact import redact

SECRETS = {
    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkq\nabc\n-----END RSA PRIVATE KEY-----",  # noqa: E501
    "aws-access-key": "key AKIAIOSFODNN7EXAMPLE here",
    "aws-secret": "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "github-token": "token ghp_" + "a1B2c3D4e5" * 4,
    "slack-token": "xoxb-1234567890-abcdefghij",
    "google-api-key": "AIza" + "SyA-1234567890abcdefghijklmnopqrstu"[:35],
    "llm-api-key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",  # noqa: E501
    "bearer-token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    "azure-account-key": "AccountKey=" + "A" * 43 + "==",
    "url-password": "postgres://admin:s3cretpass@db.example.com:5432/app",
    "credential-assignment": 'password = "correct-horse-battery"',
}


@pytest.mark.parametrize(("rule", "text"), SECRETS.items())
def test_each_secret_kind_is_masked_and_reported(rule, text):
    result = redact(f"before {text} after")
    assert rule in result.findings and "[REDACTED:" in result.text
    assert (
        result.text.startswith("before") and result.text.endswith("after")
    ) or rule == "private-key"
    leaked = [frag for frag in text.replace("=", " ").replace(":", " ").split() if len(frag) > 24]
    assert not any(frag in result.text for frag in leaked)


def test_only_the_value_of_an_assignment_is_masked():
    result = redact("db password: hunter2hunter2 and more")
    assert result.text == "db password: [REDACTED:credential-assignment] and more"


@pytest.mark.parametrize(
    "benign",
    [
        "The token count is 5 and the password field is empty.",
        "Use tokens: a, b and c to look up secrets management docs.",
        "ssh://git@github.com/org/repo.git",
        "Visit https://example.com/path?x=1 for details",
        "sk-short",
        "ghp_tooshort",
        "Range = c * t / 2 (pulse delay)",
        "api_key=abc",  # too short to be a real key
    ],
)
def test_ordinary_text_is_left_alone(benign):
    result = redact(benign)
    assert result.text == benign and not result.changed


def test_findings_never_contain_the_secret():
    secret = "ghp_" + "Zz9Yy8Xx7W" * 4
    result = redact(f"here: {secret}")
    assert secret not in repr(result.findings) and secret not in result.text


@given(st.text(max_size=500))
def test_never_raises_and_is_idempotent(text):
    once = redact(text)
    assert redact(once.text).text == once.text
