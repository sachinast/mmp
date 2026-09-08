"""Deep link validation and the deferred handshake.

The validation half of this file is the security half. A deep link destination
crosses from an untrusted source — whoever wrote the link — into code execution
on someone's phone, so the tests are written as attacks rather than as examples.
"""

from __future__ import annotations

import pytest
from mmp_core.deeplinks import MAX_DESTINATION, clean_path, is_registered_code

# Each of these, if honoured, sends the app somewhere the advertiser did not
# choose: another app's scheme, another host, or a path outside the intended
# area. They are the reason clean_path is an allowlist and not a stripper.
ATTACKS = [
    "https://evil.example",  # open redirect on our domain
    "http://evil.example",
    "//evil.example",  # protocol-relative
    "///evil.example",
    "\\\\evil.example",  # backslashes some parsers read as slashes
    "/\\evil.example",
    "otherapp://transfer?to=attacker",  # a different app entirely
    "javascript:alert(1)",
    "data:text/html,<script>",
    "/../../etc/passwd",
    "/a/../../secret",
    "/%2e%2e/%2e%2e/secret",  # encoded traversal
    "/%252e%252e/secret",  # double-encoded
    "/path\r\nLocation: https://evil.example",  # header injection
    "/path\nSet-Cookie: x=y",
    "/path\x00.png",  # null byte
    "relative/no/leading/slash",
    "",
    "   ",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_a_hostile_destination_is_refused(attack):
    assert clean_path(attack) is None, f"{attack!r} must not be honoured"


@pytest.mark.parametrize(
    "path",
    [
        "/product/123",
        "/products/summer-sale",
        "/a/b/c?utm=1&x=2",
        "/search?q=hello%20world",
        "/page#section",
        "/",
    ],
)
def test_an_ordinary_path_is_kept_exactly(path):
    """Kept verbatim, not rewritten. Sanitising would mean guessing what the
    author meant, and a wrong guess is the failure this exists to prevent."""
    assert clean_path(path) == path


def test_an_over_long_destination_is_refused():
    assert clean_path("/" + "a" * MAX_DESTINATION) is None
    assert clean_path("/" + "a" * (MAX_DESTINATION - 2)) is not None


def test_a_code_is_a_narrow_identifier():
    assert is_registered_code("summer_sale-2")
    assert not is_registered_code("summer sale")
    assert not is_registered_code("../etc")
    assert not is_registered_code("a" * 33)
    assert not is_registered_code("")
    assert not is_registered_code(None)


def test_refusal_is_total_across_every_attack():
    """A single guard for the whole set, so adding an attack above cannot be
    quietly weakened by a change that only fixes the case being edited."""
    honoured = [a for a in ATTACKS if clean_path(a) is not None]
    assert honoured == [], f"these were honoured and must not be: {honoured}"
