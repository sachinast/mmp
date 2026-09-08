"""KMS-backed credential wrapping.

The blocker this removes: the key wrapping every stored partner credential was
derived from configuration and lived in the same process as the data it
protected, which is the property KMS exists to provide.

Tested against a stand-in client rather than AWS. That is a real limitation and
worth stating: these tests exercise this module's own logic — caching, encryption
context, version handling, error containment — and prove nothing about whether
the boto3 call shape is right. The first real deployment is the first time that
is tested.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from mmp_crypto.envelope import open_sealed, organization_aad, seal
from mmp_crypto.kms import (
    ENCRYPTION_CONTEXT,
    KmsError,
    KmsMasterKeyProvider,
    provider_from_settings,
)


class FakeKms:
    """A KMS stand-in. Wraps by prefixing, which is enough to check the flow."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.encrypt_calls = 0
        self.decrypt_calls = 0
        self.contexts: list[dict] = []
        self._fail_on = fail_on
        self._store: dict[bytes, bytes] = {}

    def encrypt(self, *, KeyId, Plaintext, EncryptionContext):
        if self._fail_on == "encrypt":
            raise RuntimeError(f"AccessDeniedException for key {KeyId} as arn:aws:iam::x")
        self.encrypt_calls += 1
        self.contexts.append(EncryptionContext)
        blob = b"wrapped:" + os.urandom(8) + b":" + Plaintext
        self._store[blob] = Plaintext
        return {"CiphertextBlob": blob}

    def decrypt(self, *, CiphertextBlob, EncryptionContext):
        if self._fail_on == "decrypt":
            raise RuntimeError("InvalidCiphertextException")
        self.decrypt_calls += 1
        self.contexts.append(EncryptionContext)
        if CiphertextBlob not in self._store:
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": self._store[CiphertextBlob]}


def _provider(**kwargs) -> tuple[KmsMasterKeyProvider, FakeKms]:
    client = FakeKms(**{k: v for k, v in kwargs.items() if k == "fail_on"})
    provider = KmsMasterKeyProvider(
        client,
        key_id="alias/mmp-credentials",
        **{k: v for k, v in kwargs.items() if k != "fail_on"},
    )
    return provider, client


def test_a_credential_round_trips_through_kms():
    provider, _ = _provider()
    aad = organization_aad("org-1")
    sealed = seal(b"partner-api-token", provider=provider, aad=aad)
    assert open_sealed(sealed, provider=provider, aad=aad) == b"partner-api-token"


def test_the_plaintext_data_key_never_leaves_the_process_unwrapped():
    """seal generates the data key locally and asks KMS only to wrap it.

    Using KMS GenerateDataKey would mean the plaintext key is produced remotely;
    this way it exists only here, and the provider interface stays small enough
    that the local development implementation is a faithful stand-in rather than
    a different code path.
    """
    provider, client = _provider()
    sealed = seal(b"secret", provider=provider, aad=organization_aad("o"))

    assert client.encrypt_calls == 1, "one wrap per sealed record"
    assert client.decrypt_calls == 0, "sealing must not require a decrypt"
    assert b"secret" not in sealed.wrapped_dek, "the payload is not sent to KMS"


def test_encryption_context_is_sent_and_is_stable():
    """The context is authenticated but not secret. It binds a wrapped key to
    its purpose: the same grant cannot unwrap it under different context."""
    provider, client = _provider()
    sealed = seal(b"x", provider=provider, aad=organization_aad("o"))
    open_sealed(sealed, provider=provider, aad=organization_aad("o"))

    assert client.contexts, "context must be supplied"
    assert all(ctx == ENCRYPTION_CONTEXT for ctx in client.contexts)
    assert ENCRYPTION_CONTEXT["application"] == "mmp"


def test_unwrapping_is_cached():
    """Every webhook delivery needs its signing secret. A KMS call per delivery
    would add tens of milliseconds and a per-request bill to the outbound path.
    """
    provider, client = _provider()
    sealed = seal(b"signing-secret", provider=provider, aad=organization_aad("o"))

    for _ in range(50):
        open_sealed(sealed, provider=provider, aad=organization_aad("o"))

    assert client.decrypt_calls == 1, "fifty deliveries, one KMS call"
    assert provider.cache_hits == 49


def test_the_cache_expires():
    """The trade-off named in the module docstring: a revoked grant takes up to
    the TTL to take effect."""
    provider, client = _provider(cache_ttl=dt.timedelta(seconds=-1))
    sealed = seal(b"x", provider=provider, aad=organization_aad("o"))

    open_sealed(sealed, provider=provider, aad=organization_aad("o"))
    open_sealed(sealed, provider=provider, aad=organization_aad("o"))
    assert client.decrypt_calls == 2, "an expired entry must not be reused"


def test_the_cache_is_bounded():
    """A cache keyed by attacker-influenced input and unbounded in size is a
    memory leak with extra steps."""
    provider, _ = _provider(cache_size=5)
    for index in range(20):
        sealed = seal(f"secret-{index}".encode(), provider=provider, aad=organization_aad("o"))
        open_sealed(sealed, provider=provider, aad=organization_aad("o"))
    assert provider.cache_size <= 5


def test_the_cache_can_be_cleared_without_a_restart():
    """An incident response that requires a restart takes longer than it
    should."""
    provider, client = _provider()
    sealed = seal(b"x", provider=provider, aad=organization_aad("o"))
    open_sealed(sealed, provider=provider, aad=organization_aad("o"))

    assert provider.clear_cache() == 1
    open_sealed(sealed, provider=provider, aad=organization_aad("o"))
    assert client.decrypt_calls == 2, "the cleared key must be fetched again"


def test_a_kms_failure_does_not_leak_the_key_id_or_identity():
    """A KMS error names the key and the calling identity, and this exception
    may reach a log or an API response."""
    provider, _ = _provider(fail_on="encrypt")
    with pytest.raises(KmsError) as raised:
        seal(b"x", provider=provider, aad=organization_aad("o"))

    message = str(raised.value)
    assert "alias/mmp-credentials" not in message
    assert "arn:aws:iam" not in message
    assert message == "could not wrap the data key"


def test_an_unknown_scheme_version_is_refused():
    """A silent mismatch would surface as an authentication failure somewhere
    far away from the cause."""
    provider, _ = _provider()
    sealed = seal(b"x", provider=provider, aad=organization_aad("o"))
    from mmp_crypto.envelope import SealedSecret

    orphaned = SealedSecret(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_dek=sealed.wrapped_dek,
        key_version=99,
    )
    with pytest.raises(KeyError):
        open_sealed(orphaned, provider=provider, aad=organization_aad("o"))


def test_a_wrong_sized_data_key_is_refused():
    provider, _ = _provider()
    with pytest.raises(ValueError, match="32 bytes"):
        provider.wrap(b"too short")


def test_a_key_id_is_required():
    with pytest.raises(ValueError, match="key id"):
        KmsMasterKeyProvider(FakeKms(), key_id="")


# --- provider selection -------------------------------------------------
class _Settings:
    def __init__(self, **kwargs):
        self.session_secret = "s" * 64
        self.kms_key_id = None
        self.kms_region = None
        self.is_prod = False
        self.__dict__.update(kwargs)


def test_production_refuses_to_start_without_kms():
    """The blocker, enforced where the provider is constructed rather than in a
    checklist. A control that depends on someone remembering is not a control.
    """
    with pytest.raises(RuntimeError, match="MMP_KMS_KEY_ID is required"):
        provider_from_settings(_Settings(is_prod=True))


def test_development_falls_back_to_the_local_provider():
    from mmp_crypto.envelope import LocalMasterKeyProvider

    provider = provider_from_settings(_Settings(is_prod=False))
    assert isinstance(provider, LocalMasterKeyProvider)


def test_a_configured_key_id_selects_kms(monkeypatch):
    from mmp_crypto import kms

    monkeypatch.setattr(kms, "boto3_client", lambda region=None: FakeKms())
    provider = provider_from_settings(_Settings(kms_key_id="alias/mmp", kms_region="eu-west-1"))
    assert isinstance(provider, KmsMasterKeyProvider)


def test_a_configured_key_id_is_used_in_development_too():
    """No environment-dependent branch beyond the production requirement.

    If KMS is configured it is used, so staging exercises the same code path
    production will — the alternative is discovering the integration works only
    where it was never tested.
    """
    from mmp_crypto import kms

    original = kms.boto3_client
    kms.boto3_client = lambda region=None: FakeKms()  # type: ignore[assignment]
    try:
        provider = provider_from_settings(_Settings(kms_key_id="alias/x", is_prod=False))
        assert isinstance(provider, KmsMasterKeyProvider)
    finally:
        kms.boto3_client = original  # type: ignore[assignment]
