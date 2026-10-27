from hive_mcp.slack.verify import WINDOW_SECONDS, sign, verify

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def test_signature_matches_slacks_documented_example():
    body = (
        b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
        b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect"
        b"&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
        b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
    )
    assert (
        sign(SECRET, "1531420618", body)
        == "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
    )


def test_old_future_malformed_or_forged_requests_are_refused():
    now = 1_700_000_000
    body = b"a=1"
    good = sign(SECRET, str(now), body)
    assert verify(SECRET, str(now), good, body, lambda: now)
    assert verify(SECRET, str(now), good, body, lambda: now + WINDOW_SECONDS)
    assert not verify(SECRET, str(now), good, body, lambda: now + WINDOW_SECONDS + 1)
    assert not verify(SECRET, str(now), good, body, lambda: now - WINDOW_SECONDS - 1)
    assert verify(SECRET, str(now), good, body, lambda: now - WINDOW_SECONDS)
    assert not verify(SECRET, str(now), good, b"a=2", lambda: now)
    assert not verify("other-secret", str(now), good, body, lambda: now)
    for ts, sig in ((None, good), (str(now), None), ("abc", good), ("", good), ("-5", good)):
        assert not verify(SECRET, ts, sig, body, lambda: now)
    assert not verify(SECRET, str(now), good[:-1], body, lambda: now)  # a truncated signature


def test_a_timestamp_of_non_ascii_digits_is_refused_not_a_crash():
    assert verify("s", "²", "v0=00", b"") is False
    assert verify("s", "١٢٣", "v0=00", b"") is False  # Arabic-Indic digits
