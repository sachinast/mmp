"""Envelope encryption for stored partner credentials.

Meta tokens, TikTok secrets, affiliate network passwords — credentials that we
must be able to *recover*, unlike a password or an API key. Hashing is not an
option, so the question becomes where the key lives.

The pattern: a fresh 256-bit data key (DEK) per record encrypts the payload with
AES-256-GCM; the DEK itself is encrypted by a master key held in KMS and stored
alongside the ciphertext. Three properties follow:

* A database dump yields nothing without KMS access.
* Rotating the master key rewraps DEKs — it does not re-encrypt payloads. A
  rotation is a fast metadata update, so it actually gets done.
* ``key_version`` lets old and new master keys coexist during a rotation, so
  rotation is not an outage.

Additional authenticated data binds each ciphertext to the organisation that
owns it. A row copied into another tenant's record fails to decrypt rather than
quietly working — the AAD makes tenancy part of the integrity check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_BYTES = 32
NONCE_BYTES = 12  # GCM standard; never reuse one under the same key.


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_version: int


class MasterKeyProvider(Protocol):
    """The seam where a real KMS goes.

    Kept as a Protocol so that the production implementation (AWS KMS, Vault)
    can be dropped in without any call site changing, and so tests do not need
    a cloud dependency to exercise the encryption path itself.
    """

    @property
    def current_version(self) -> int: ...

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, version: int) -> bytes: ...


class LocalMasterKeyProvider:
    """Development and test provider.

    Wraps DEKs with a local AES-GCM key derived from configuration. Adequate for
    a laptop, explicitly **not** adequate for production: the master key sits in
    the same process as the data it protects, which is the entire property KMS
    exists to provide. Production wiring is a Phase 11 deliverable.
    """

    def __init__(self, master_keys: dict[int, bytes], current_version: int) -> None:
        if current_version not in master_keys:
            raise ValueError("current_version has no corresponding master key")
        for version, key in master_keys.items():
            if len(key) != DEK_BYTES:
                raise ValueError(f"master key v{version} must be {DEK_BYTES} bytes")
        self._keys = master_keys
        self._current = current_version

    @property
    def current_version(self) -> int:
        return self._current

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        aead = AESGCM(self._keys[self._current])
        return nonce + aead.encrypt(nonce, dek, b"dek-wrap")

    def unwrap(self, wrapped: bytes, version: int) -> bytes:
        key = self._keys.get(version)
        if key is None:
            raise KeyError(f"master key version {version} is not available")
        nonce, body = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, body, b"dek-wrap")


def seal(plaintext: bytes, *, provider: MasterKeyProvider, aad: bytes) -> SealedSecret:
    dek = os.urandom(DEK_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return SealedSecret(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_dek=provider.wrap(dek),
        key_version=provider.current_version,
    )


def open_sealed(sealed: SealedSecret, *, provider: MasterKeyProvider, aad: bytes) -> bytes:
    dek = provider.unwrap(sealed.wrapped_dek, sealed.key_version)
    return AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad)


def organization_aad(organization_id: object) -> bytes:
    """Bind a ciphertext to its owning organisation."""
    return f"org:{organization_id}".encode()


def rewrap(sealed: SealedSecret, *, old: MasterKeyProvider, new: MasterKeyProvider) -> SealedSecret:
    """Rotate the master key without re-encrypting the payload."""
    dek = old.unwrap(sealed.wrapped_dek, sealed.key_version)
    return SealedSecret(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_dek=new.wrap(dek),
        key_version=new.current_version,
    )


def provider_from_settings(settings: object) -> MasterKeyProvider:
    """Resolve the credential-wrapping key from configuration.

    Lives here rather than in a service because **both** the API and the worker
    need it, and they must derive the same key: the API seals a webhook's
    signing secret, the worker opens it to sign a delivery. If those two ever
    disagreed, every secret written by one would be undecryptable by the other,
    and the symptom would be silent delivery failures rather than an error at
    startup.

    In production this returns a KMS-backed provider (Phase 11). Until then the
    local provider derives a key from configuration — and refuses to do so
    outside development, so the weaker path cannot reach production by accident.
    """
    from hashlib import sha256

    if getattr(settings, "is_prod", False):
        raise RuntimeError(
            "no KMS master key provider configured — refusing to start in production "
            "with process-local credential encryption"
        )
    material = sha256(str(settings.session_secret).encode()).digest()  # type: ignore[attr-defined]
    return LocalMasterKeyProvider({1: material}, 1)
