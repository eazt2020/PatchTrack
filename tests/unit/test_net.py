"""Client address parsing.

The address is written to `inet` columns. A value that is not an address must
become NULL, never a failed request or a recorded lie.
"""

from __future__ import annotations

from patchtrack_common.net import parse_ip


def test_ipv4_is_canonicalised():
    assert parse_ip("10.0.0.1") == "10.0.0.1"
    assert parse_ip(" 10.0.0.1 ") == "10.0.0.1"


def test_ipv6_is_canonicalised():
    assert parse_ip("2001:DB8::1") == "2001:db8::1"


def test_arbitrary_header_text_is_not_an_address():
    # This is what a TestClient peer looks like; it must not reach an inet
    # column.
    assert parse_ip("testclient") is None
    assert parse_ip("not an ip") is None
    assert parse_ip("") is None
    assert parse_ip(None) is None


def test_a_long_value_cannot_smuggle_anything():
    assert parse_ip("127.0.0.1, 10.0.0.1") is None
    assert parse_ip("127.0.0.1/24") is None
