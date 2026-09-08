"""KMS-backed master keys.

The blocker this removes: until now the key that wraps every stored partner
credential was derived from configuration and lived in the same process as the
data it protected. That is precisely the property a key management service
exists to provide, and `provider_from_settings` refuses to start in production
without one.

**The shape of the integration.** ``seal`` generates a data key locally and asks
the provider to wrap it. That is deliberate rather than using KMS
``GenerateDataKey``: the plaintext data key then exists only in our process and
never travels to AWS, and the provider interface stays small enough that the
local development implementation is a faithful stand-in rather than a different
code path. The cost is one extra ``Encrypt`` call per sealed secret, which is
paid when a credential is *stored* — a rare event — not when it is used.

**Caching.** Unwrapping is not rare: every webhook delivery needs its signing
secret. A KMS call per delivery would add tens of milliseconds and a per-request
bill to the outbound path. So unwrapped data keys are cached in memory, bounded
by both size and age.

That cache is a real trade-off and worth naming: it means a revoked KMS grant
takes up to the TTL to take effect, and it means plaintext data keys sit in
process memory for that long. Both are the normal cost of using envelope
encryption at request rate; the mitigation is that the TTL is short, the cache is
per-process, and it holds data keys rather than the master key — compromising it
exposes the credentials in flight, not every credential ever stored.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import OrderedDict
from typing import Any, Protocol

from mmp_core.logging import get_logger

from mmp_crypto.envelope import DEK_BYTES, MasterKeyProvider

log = get_logger(__name__)

# Short enough that a revoked grant takes effect in minutes; long enough that a
# busy worker is not making a KMS call per delivery.
DEFAULT_CACHE_TTL = dt.timedelta(minutes=5)
DEFAULT_CACHE_SIZE = 1000

# KMS encryption context. It is authenticated but not secret, and it binds a
# wrapped key to its purpose: a data key wrapped for this platform cannot be
# unwrapped by a caller supplying different context, even with the same grant.
ENCRYPTION_CONTEXT = {"application": "mmp", "purpose": "credential-wrapping"}


class KmsClient(Protocol):
    """The subset of the KMS API used here.

    A Protocol rather than boto3 directly so the tests exercise this module's
    own logic — caching, context, error handling — without a network or an AWS
    account, and so a different provider can be dropped in.
    """

    def encrypt(self, **kwargs: Any) -> dict[str, Any]: ...

    def decrypt(self, **kwargs: Any) -> dict[str, Any]: ...


class KmsError(Exception):
    """A key operation failed. Never carries the plaintext or the key material."""


class KmsMasterKeyProvider:
    """Wraps data keys with a customer master key held in KMS.

    ``key_version`` is carried in the sealed record but is not the KMS key id:
    KMS manages its own key rotation transparently, and a ``Decrypt`` call needs
    no key id at all. The version exists so that a *scheme* change — a different
    algorithm, a different context — can be migrated without ambiguity.
    """

    def __init__(
        self,
        client: KmsClient,
        *,
        key_id: str,
        version: int = 1,
        cache_ttl: dt.timedelta = DEFAULT_CACHE_TTL,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if not key_id:
            raise ValueError("a KMS key id or alias is required")
        self._client = client
        self._key_id = key_id
        self._version = version
        self._cache_ttl = cache_ttl
        self._cache_size = cache_size
        # An OrderedDict as an LRU, under a lock: workers unwrap concurrently,
        # and a dict mutated from several tasks is a dict that eventually raises
        # during iteration.
        self._cache: OrderedDict[bytes, tuple[bytes, dt.datetime]] = OrderedDict()
        self._lock = threading.Lock()
        self.calls_made = 0
        self.cache_hits = 0

    @property
    def current_version(self) -> int:
        return self._version

    def wrap(self, dek: bytes) -> bytes:
        if len(dek) != DEK_BYTES:
            raise ValueError(f"a data key must be {DEK_BYTES} bytes")
        try:
            response = self._client.encrypt(
                KeyId=self._key_id,
                Plaintext=dek,
                EncryptionContext=ENCRYPTION_CONTEXT,
            )
        except Exception as exc:
            # Deliberately not chaining the original message into the raised
            # text: a KMS error can name the key id and the caller identity, and
            # this exception may reach a log or an API response.
            log.exception("kms_wrap_failed", key_id=self._key_id)
            raise KmsError("could not wrap the data key") from exc
        self.calls_made += 1
        return bytes(response["CiphertextBlob"])

    def unwrap(self, wrapped: bytes, version: int) -> bytes:
        if version != self._version:
            # A record sealed under a different scheme. Refused rather than
            # attempted: a silent mismatch would surface as an authentication
            # failure somewhere far away from the cause.
            raise KeyError(f"master key version {version} is not available")

        cached = self._cached(wrapped)
        if cached is not None:
            self.cache_hits += 1
            return cached

        try:
            response = self._client.decrypt(
                CiphertextBlob=wrapped,
                EncryptionContext=ENCRYPTION_CONTEXT,
            )
        except Exception as exc:
            log.exception("kms_unwrap_failed")
            raise KmsError("could not unwrap the data key") from exc

        self.calls_made += 1
        dek = bytes(response["Plaintext"])
        self._remember(wrapped, dek)
        return dek

    # --- cache ----------------------------------------------------------
    def _cached(self, wrapped: bytes) -> bytes | None:
        now = dt.datetime.now(dt.UTC)
        with self._lock:
            entry = self._cache.get(wrapped)
            if entry is None:
                return None
            dek, expires = entry
            if now >= expires:
                # Expired entries are removed on read rather than swept: a
                # background sweeper over a bounded cache is more machinery than
                # the problem deserves.
                del self._cache[wrapped]
                return None
            self._cache.move_to_end(wrapped)
            return dek

    def _remember(self, wrapped: bytes, dek: bytes) -> None:
        with self._lock:
            self._cache[wrapped] = (dek, dt.datetime.now(dt.UTC) + self._cache_ttl)
            self._cache.move_to_end(wrapped)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def clear_cache(self) -> int:
        """Drop every cached data key.

        The lever to pull when a grant is revoked and waiting out the TTL is not
        acceptable. Exposed deliberately: an incident response that requires a
        restart is one that takes longer than it should.
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        log.warning("kms_cache_cleared", entries=count)
        return count

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)


def boto3_client(region: str | None = None) -> KmsClient:
    """A real KMS client.

    Imported lazily so that boto3 is not a hard dependency of every service —
    the tracker never wraps a credential and has no reason to carry an AWS SDK.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover — deployment concern
        raise KmsError("boto3 is required for KMS support: install the 'kms' extra") from exc
    client: KmsClient = boto3.client("kms", region_name=region)
    return client


def provider_from_settings(settings: object) -> MasterKeyProvider:
    """Choose a master key provider from configuration.

    Production requires a KMS key id and refuses to start without one. That
    check lives here, at the single place a provider is constructed, rather than
    in a deployment checklist — a control that depends on someone remembering is
    not a control.
    """
    key_id = getattr(settings, "kms_key_id", None)
    is_prod = getattr(settings, "is_prod", False)

    if key_id:
        region = getattr(settings, "kms_region", None)
        log.info("kms_provider_configured", key_id=key_id, region=region)
        return KmsMasterKeyProvider(boto3_client(region), key_id=key_id)

    if is_prod:
        raise RuntimeError(
            "MMP_KMS_KEY_ID is required in production: credential wrapping must "
            "not use a key derived from configuration and held in the same "
            "process as the data it protects"
        )

    from mmp_crypto.envelope import provider_from_settings as local_provider

    return local_provider(settings)
