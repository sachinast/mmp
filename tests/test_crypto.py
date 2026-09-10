"""Cryptographic primitives, tested for the properties they were chosen for."""

from __future__ import annotations

import datetime as dt
import os
import time

import pytest
from cryptography.exceptions import InvalidTag
from mmp_crypto.envelope import (
    LocalMasterKeyProvider,
    SealedSecret,
    open_sealed,
    organization_aad,
    rewrap,
    seal,
)
from mmp_crypto.passwords import (
    PasswordPolicyError,
    hash_password,
    verify_password,
    verify_password_constant_work,
)
from mmp_crypto.pii import hash_device_id, hash_ip, truncate_ip_for_geo


# --- passwords ----------------------------------------------------------
def test_password_roundtrip():
    stored = hash_password("correct-horse-battery-staple")
    assert verify_password(stored, "correct-horse-battery-staple")
    assert not verify_password(stored, "correct-horse-battery-stapl")


def test_hash_is_salted():
    """Identical passwords must not produce identical hashes."""
    assert hash_password("same-password-here") != hash_password("same-password-here")


def test_hash_does_not_contain_the_password():
    stored = hash_password("very-distinctive-password")
    assert "very-distinctive-password" not in stored


def test_argon2id_is_used():
    assert hash_password("a-valid-password").startswith("$argon2id$")


@pytest.mark.parametrize("bad", ["", "short", "eleven-chr"])
def test_short_passwords_rejected(bad):
    with pytest.raises(PasswordPolicyError):
        hash_password(bad)


def test_absurdly_long_password_rejected():
    """Argon2 reads the whole input; unbounded input is unbounded work."""
    with pytest.raises(PasswordPolicyError):
        hash_password("x" * 5000)


def test_missing_user_costs_the_same_as_a_wrong_password():
    """Otherwise response time enumerates which emails have accounts."""
    stored = hash_password("a-real-users-password")

    def timed(fn) -> float:
        start = time.perf_counter()
        fn()
        return time.perf_counter() - start

    absent = timed(lambda: verify_password_constant_work(None, "guess-the-password"))
    present = timed(lambda: verify_password_constant_work(stored, "guess-the-password"))

    # Both perform one full argon2 verification, so they should be the same
    # order of magnitude. A 10x gap would be a usable oracle.
    assert 0.1 < absent / present < 10, f"timing gap: absent={absent:.4f}s present={present:.4f}s"


# --- envelope encryption ------------------------------------------------
def _provider(versions=(1,)):
    return LocalMasterKeyProvider({v: os.urandom(32) for v in versions}, max(versions))


def test_seal_open_roundtrip():
    provider = _provider()
    aad = organization_aad("org-1")
    sealed = seal(b"partner-api-token", provider=provider, aad=aad)
    assert open_sealed(sealed, provider=provider, aad=aad) == b"partner-api-token"


def test_ciphertext_does_not_contain_the_plaintext():
    provider = _provider()
    sealed = seal(b"unmistakable-secret", provider=provider, aad=organization_aad("o"))
    assert b"unmistakable-secret" not in sealed.ciphertext


def test_credential_is_bound_to_its_organization():
    """A row copied into another tenant must fail, not silently decrypt."""
    provider = _provider()
    sealed = seal(b"token", provider=provider, aad=organization_aad("org-a"))
    with pytest.raises(InvalidTag):
        open_sealed(sealed, provider=provider, aad=organization_aad("org-b"))


def test_tampering_is_detected():
    """AES-GCM authenticates; a flipped bit must not decrypt to garbage."""
    provider = _provider()
    aad = organization_aad("org-a")
    sealed = seal(b"token", provider=provider, aad=aad)
    corrupted = type(sealed)(
        ciphertext=bytes([sealed.ciphertext[0] ^ 0x01, *sealed.ciphertext[1:]]),
        nonce=sealed.nonce,
        wrapped_dek=sealed.wrapped_dek,
        key_version=sealed.key_version,
    )
    with pytest.raises(InvalidTag):
        open_sealed(corrupted, provider=provider, aad=aad)


def test_each_record_gets_its_own_data_key():
    """A shared DEK would make one compromise a total compromise."""
    provider = _provider()
    aad = organization_aad("org-a")
    first = seal(b"same-plaintext", provider=provider, aad=aad)
    second = seal(b"same-plaintext", provider=provider, aad=aad)
    assert first.wrapped_dek != second.wrapped_dek
    assert first.ciphertext != second.ciphertext


def test_rotation_rewraps_without_reencrypting():
    """Rotation must be a metadata update, or it never actually gets done."""
    old = LocalMasterKeyProvider({1: os.urandom(32)}, 1)
    keys = {1: old._keys[1], 2: os.urandom(32)}
    new = LocalMasterKeyProvider(keys, 2)

    aad = organization_aad("org-a")
    sealed = seal(b"long-lived-credential", provider=old, aad=aad)
    rotated = rewrap(sealed, old=old, new=new)

    assert rotated.ciphertext == sealed.ciphertext, "the payload must not be re-encrypted"
    assert rotated.key_version == 2
    assert open_sealed(rotated, provider=new, aad=aad) == b"long-lived-credential"


def test_unknown_key_version_fails_loudly():
    provider = _provider()
    sealed = seal(b"x", provider=provider, aad=organization_aad("o"))
    orphaned = type(sealed)(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_dek=sealed.wrapped_dek,
        key_version=99,
    )
    with pytest.raises(KeyError):
        open_sealed(orphaned, provider=provider, aad=organization_aad("o"))


# --- PII ----------------------------------------------------------------
def test_ip_hash_is_stable_within_a_day():
    day = dt.date(2026, 9, 7)
    assert hash_ip("203.0.113.9", pepper="p" * 64, day=day) == hash_ip(
        "203.0.113.9", pepper="p" * 64, day=day
    )


def test_ip_hash_rotates_daily():
    """Caps correlation to a single day, even for us."""
    pepper = "p" * 64
    monday = hash_ip("203.0.113.9", pepper=pepper, day=dt.date(2026, 9, 7))
    tuesday = hash_ip("203.0.113.9", pepper=pepper, day=dt.date(2026, 9, 8))
    assert monday != tuesday


def test_ipv4_mapped_ipv6_matches_its_ipv4_form():
    """Otherwise a dual-stack client silently fails to match its own click."""
    pepper = "p" * 64
    day = dt.date(2026, 9, 7)
    assert hash_ip("::ffff:203.0.113.9", pepper=pepper, day=day) == hash_ip(
        "203.0.113.9", pepper=pepper, day=day
    )


@pytest.mark.parametrize("bad", ["", "not-an-ip", "999.999.999.999", "1.2.3"])
def test_malformed_ip_returns_none(bad):
    assert hash_ip(bad, pepper="p" * 64) is None


def test_opted_out_advertising_id_is_not_hashed():
    """Android returns all-zeros for a user who opted out.

    Hashing it would create one bucket that matches every opted-out device on
    the platform to every other one — attribution's worst possible outcome.
    """
    assert hash_device_id("00000000-0000-0000-0000-000000000000", pepper="p" * 64) is None
    assert hash_device_id("", pepper="p" * 64) is None


def test_device_id_is_case_insensitive():
    pepper = "p" * 64
    assert hash_device_id("AB-CD-EF", pepper=pepper) == hash_device_id("ab-cd-ef", pepper=pepper)


def test_geo_truncation_drops_host_precision():
    assert truncate_ip_for_geo("203.0.113.99") == "203.0.113.0"
    assert truncate_ip_for_geo("2001:db8:1234:5678::1") == "2001:db8:1234::"


def test_a_secret_sealed_after_rotation_cannot_be_opened_as_version_one():
    """Why every sealed-secret table has to carry its key version.

    `seal()` wraps the data key with whichever master key is current and returns
    that version. Discarding it and assuming 1 works right up until someone
    rotates: the data key is then wrapped under the new master key and unwrapped
    under the old one, and AES-GCM answers that with InvalidTag rather than
    anything diagnosable.

    This is what the webhooks table was doing. The failure surfaced as deliveries
    abandoned with "signing secret could not be decrypted" — the sender refusing
    to send unsigned, which is right, for a reason nothing explained.
    """
    from cryptography.exceptions import InvalidTag

    old_key, new_key = os.urandom(32), os.urandom(32)
    after_rotation = LocalMasterKeyProvider({1: old_key, 2: new_key}, current_version=2)
    aad = organization_aad("org-a")

    sealed = seal(b"whsec_example", provider=after_rotation, aad=aad)
    assert sealed.key_version == 2, "sealed under the key that is current now"

    assuming_version_one = SealedSecret(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_dek=sealed.wrapped_dek,
        key_version=1,
    )
    with pytest.raises(InvalidTag):
        open_sealed(assuming_version_one, provider=after_rotation, aad=aad)

    assert open_sealed(sealed, provider=after_rotation, aad=aad) == b"whsec_example"


def test_every_table_holding_a_sealed_secret_records_its_key_version():
    """A structural guard, because this was found by reading rather than by
    failing: `provider_integrations` carried a key_version and `webhooks` did
    not, and nothing compared them."""
    from mmp_db.models import distribution, governance

    sealed_columns = ("wrapped_dek",)
    for module in (distribution, governance):
        for name in dir(module):
            model = getattr(module, name)
            table = getattr(model, "__table__", None)
            if table is None:
                continue
            if not any(column in table.columns for column in sealed_columns):
                continue
            assert "key_version" in table.columns, (
                f"{table.name} stores a wrapped data key but not the master key "
                f"version that wrapped it — it cannot survive a rotation"
            )
