"""SKAdNetwork postback verification.

The five postbacks below are Apple's own, published in their documentation
together with the signatures Apple's private key actually produced. That makes
this conformance testing rather than self-consistency testing: a suite that
signs its own fixtures with its own key proves the ECDSA call works and proves
nothing at all about whether the canonical string matches Apple's — which is the
only part that can realistically be wrong.

That distinction matters more here than almost anywhere else in this codebase.
A postback arrives with no credential; the signature is the entire
authentication. A verifier built on a subtly wrong field order rejects every
genuine postback, and the tempting fix for "nothing verifies" is to stop
verifying.
"""

from __future__ import annotations

import base64
import copy

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from mmp_attrib.skadnetwork import (
    SEPARATOR,
    Rejected,
    Rejection,
    Verified,
    canonical_string,
    verify,
)

# --- Apple's published postbacks, with Apple's real signatures ---------------
V4_WEB_HIGH = {
    "version": "4.0",
    "ad-network-id": "com.example",
    "source-identifier": "5239",
    "app-id": 525463029,
    "transaction-id": "6aafb7a5-0170-41b5-bbe4-fe71dedf1e30",
    "redownload": False,
    "source-domain": "example.com",
    "fidelity-type": 1,
    "did-win": True,
    "conversion-value": 63,
    "postback-sequence-index": 0,
    "attribution-signature": (
        "MEUCIGRmSMrqedNu6uaHyhVcifs118R5z/AB6cvRaKrRRHWRAiEAv96ne3dKQ5kJpbsfk4eYiePmrZUU6sQmo+7zfP/1Bxo="
    ),
}

V4_WEB_LOW = {
    "version": "4.0",
    "ad-network-id": "com.example",
    "source-identifier": "39",
    "app-id": 525463029,
    "transaction-id": "6aafb7a5-0170-41b5-bbe4-fe71dedf1e31",
    "redownload": False,
    "source-domain": "example.com",
    "fidelity-type": 1,
    "did-win": True,
    "coarse-conversion-value": "high",
    "postback-sequence-index": 0,
    "attribution-signature": (
        "MEUCIQD4rX6eh38qEhuUKHdap345UbmlzA7KEZ1bhWZuYM8MJwIgMnyiiZe6heabDkGwOaKBYrUXQhKtF3P/ERHqkR/XpuA="
    ),
}

V3_WINNER = {
    "version": "3.0",
    "ad-network-id": "example123.skadnetwork",
    "campaign-id": 42,
    "transaction-id": "6aafb7a5-0170-41b5-bbe4-fe71dedf1e28",
    "app-id": 525463029,
    "attribution-signature": (
        "MEYCIQD5eq3AUlamORiGovqFiHWI4RZT/PrM3VEiXUrsC+M51wIhAPMANZA9c07raZJ64gVaXhB9+9yZj/X6DcNxONdccQij"
    ),
    "redownload": True,
    "source-app-id": 1234567891,
    "fidelity-type": 1,
    "conversion-value": 20,
    "did-win": True,
}

V3_LOSER = {
    "version": "3.0",
    "ad-network-id": "example123.skadnetwork",
    "campaign-id": 42,
    "transaction-id": "f9ac267a-a889-44ce-b5f7-0166d11461f0",
    "app-id": 525463029,
    "attribution-signature": (
        "MEUCIQDDetUtkyc/MiQvVJ5I6HIO1E7l598572Wljot2Onzd4wIgVJLzVcyAV+TXksGNoa0DTMXEPgNPeHCmD4fw1ABXX0g="
    ),
    "redownload": True,
    "fidelity-type": 1,
    "did-win": False,
}

V2_2 = {
    "version": "2.2",
    "ad-network-id": "com.example",
    "campaign-id": 42,
    "transaction-id": "6aafb7a5-0170-41b5-bbe4-fe71dedf1e28",
    "app-id": 525463029,
    "attribution-signature": (
        "MEYCIQDTuQ1Z4Tpy9D3aEKbxLl5J5iKiTumcqZikuY/AOD2U7QIhAJAaiAv89AoquHXJffcieEQXdWHpcV8ZgbKN0EwV9/sY"
    ),
    "redownload": True,
    "source-app-id": 1234567891,
    "fidelity-type": 1,
    "conversion-value": 20,
}

REAL = {
    "4.0 web, high tier": V4_WEB_HIGH,
    "4.0 web, low tier": V4_WEB_LOW,
    "3.0 winner": V3_WINNER,
    "3.0 non-winner": V3_LOSER,
    "2.2": V2_2,
}


# --- conformance ------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(REAL))
def test_apples_own_postbacks_verify(name: str) -> None:
    """Against Apple's published key, with Apple's published signatures. If any
    of these fails, the canonical string does not match Apple's and no genuine
    postback would ever be accepted in production."""
    result = verify(REAL[name])
    assert isinstance(result, Verified), getattr(result, "detail", result)


def test_a_non_winning_postback_is_valid_but_did_not_win() -> None:
    """Validity and winning are different questions. A signed non-winner is a
    real message from Apple saying this network lost — worth storing, not worth
    counting as an install."""
    result = verify(V3_LOSER)
    assert isinstance(result, Verified)
    assert result.did_win is False


def test_the_two_names_for_the_campaign_resolve_to_one_field() -> None:
    """Version 4 renamed campaign-id to source-identifier."""
    assert verify(V3_WINNER).source_identifier == "42"  # type: ignore[union-attr]
    assert verify(V4_WEB_HIGH).source_identifier == "5239"  # type: ignore[union-attr]


# --- tampering --------------------------------------------------------------
@pytest.mark.parametrize(
    "field",
    [
        "ad-network-id",
        "source-identifier",
        "app-id",
        "transaction-id",
        "redownload",
        "source-domain",
        "fidelity-type",
        "did-win",
        "postback-sequence-index",
    ],
)
def test_changing_any_signed_field_breaks_verification(field: str) -> None:
    tampered = copy.deepcopy(V4_WEB_HIGH)
    original = tampered[field]
    tampered[field] = False if original is True else "999" if original != "999" else "998"

    result = verify(tampered)
    assert isinstance(result, Rejected)
    assert result.reason is Rejection.BAD_SIGNATURE


def test_the_conversion_value_is_not_covered_by_the_signature() -> None:
    """A limitation of Apple's design, asserted so it stays known.

    The signature covers neither conversion-value nor coarse-conversion-value in
    any version, so a genuine postback can be replayed with a different value and
    still verify. Downstream code must treat the value as reported-but-unproven,
    and the transaction-id uniqueness constraint is what limits the damage.
    """
    replayed = copy.deepcopy(V4_WEB_HIGH)
    replayed["conversion-value"] = 7

    result = verify(replayed)
    assert isinstance(result, Verified), "unsigned fields do not affect validity"
    assert result.conversion_value == 7


def test_a_signature_from_another_key_is_refused() -> None:
    """The forgery that matters: an attacker who signs a well-formed postback
    with a key of their own."""
    attacker = ec.generate_private_key(ec.SECP256R1())
    forged = copy.deepcopy(V4_WEB_HIGH)
    forged["transaction-id"] = "11111111-1111-1111-1111-111111111111"
    message = canonical_string(forged).encode()
    forged["attribution-signature"] = base64.b64encode(
        attacker.sign(message, ec.ECDSA(hashes.SHA256()))
    ).decode()

    result = verify(forged)
    assert isinstance(result, Rejected)
    assert result.reason is Rejection.BAD_SIGNATURE

    # And it verifies under the attacker's own key, proving the forgery was
    # well-formed and that only the key rejected it.
    assert isinstance(verify(forged, key=attacker.public_key()), Verified)


def test_a_value_containing_the_separator_is_refused() -> None:
    """Fields are joined, not length-prefixed, so a value carrying the separator
    moves the boundary between two fields: one signature, two readings. An
    attacker who could do this would keep a real signature while changing which
    campaign was credited."""
    smuggled = copy.deepcopy(V4_WEB_HIGH)
    smuggled["ad-network-id"] = f"com.example{SEPARATOR}5239"

    result = verify(smuggled)
    assert isinstance(result, Rejected)
    assert result.reason is Rejection.SEPARATOR_IN_VALUE


# --- malformed input --------------------------------------------------------
def test_an_unsigned_postback_is_refused() -> None:
    unsigned = {k: v for k, v in V4_WEB_HIGH.items() if k != "attribution-signature"}
    assert verify(unsigned).reason is Rejection.MISSING_SIGNATURE  # type: ignore[union-attr]


def test_a_signature_that_is_not_base64_is_refused() -> None:
    broken = copy.deepcopy(V4_WEB_HIGH)
    broken["attribution-signature"] = "not base64!!"
    assert verify(broken).reason is Rejection.MALFORMED_SIGNATURE  # type: ignore[union-attr]


def test_versions_signed_with_keys_we_do_not_hold_are_refused() -> None:
    """1.0 and 2.0 use keys issued through Apple's registration portal. A
    postback we cannot verify is one we cannot count."""
    for version in ("1.0", "2.0"):
        old = copy.deepcopy(V2_2)
        old["version"] = version
        result = verify(old)
        assert isinstance(result, Rejected)
        assert result.reason is Rejection.UNSUPPORTED_VERSION


def test_a_future_version_is_refused_rather_than_guessed() -> None:
    """Apple adds versions with new fields in new positions. Guessing an order
    would either reject everything or, worse, accept a string that is not what
    was signed."""
    future = copy.deepcopy(V4_WEB_HIGH)
    future["version"] = "5.0"
    assert verify(future).reason is Rejection.UNKNOWN_VERSION  # type: ignore[union-attr]


def test_a_postback_missing_a_required_field_is_refused() -> None:
    incomplete = {k: v for k, v in V4_WEB_HIGH.items() if k != "app-id"}
    result = verify(incomplete)
    assert isinstance(result, Rejected)
    assert result.reason is Rejection.MISSING_FIELD


def test_an_empty_or_junk_payload_is_refused_without_raising() -> None:
    """This endpoint is public. Anything at all can arrive at it."""
    for payload in ({}, {"version": ""}, {"version": None}, {"junk": "x"}):
        result = verify(payload)  # type: ignore[arg-type]
        assert isinstance(result, Rejected)


# --- the canonical string ---------------------------------------------------
def test_an_absent_optional_field_is_omitted_not_blanked() -> None:
    """Apple omits it entirely. Including it as an empty string would add a
    separator Apple never signed over — and the non-winner vector, which has no
    source-app-id, is what proves this against a real signature."""
    assert SEPARATOR * 2 not in canonical_string(V3_LOSER)
    assert isinstance(verify(V3_LOSER), Verified)


def test_booleans_are_rendered_as_apple_renders_them() -> None:
    assert "true" in canonical_string(V3_WINNER).split(SEPARATOR)
    assert "false" in canonical_string(V4_WEB_HIGH).split(SEPARATOR)
