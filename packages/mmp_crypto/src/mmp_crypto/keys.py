"""API key generation and verification.

The requirement that shapes everything here: this runs on **every ingest
request**. An argon2 verification per event batch would cap the platform at a
few hundred requests per second on hashing alone — we would have built our own
denial of service.

So the scheme is:

* 32 bytes from ``secrets.token_bytes`` — the entropy does the work a slow hash
  would otherwise have to do. There is nothing to brute-force offline in
  reasonable time, so a fast hash is safe here in a way it never is for a
  human-chosen password.
* Stored as HMAC-SHA256 under a **pepper held in KMS**, not in the database. A
  dump of the ``api_keys`` table alone is useless: the attacker still needs the
  pepper, which lives in a different trust domain.
* A plaintext ``key_prefix``, indexed, so authentication is one indexed lookup
  and one constant-time comparison — not a scan over every key in the table
  computing hashes until one matches.

The raw key exists in memory exactly once, is returned exactly once, and is
never written to a log, a response, or a database column.
"""

from __future__ import annotations

import hmac
import math
import secrets
import string
from dataclasses import dataclass
from hashlib import sha256

# Base62, deliberately not base64url.
#
# token_urlsafe emits '-' and '_', and '_' is the delimiter in the key format
# below — so roughly half of all generated keys could not be split back apart.
# The alphabet and the delimiter must not overlap; base62 also survives being
# double-clicked, pasted into a URL, or read down a phone line.
ALPHABET = string.ascii_letters + string.digits
_BITS_PER_CHAR = math.log2(len(ALPHABET))

# Long enough to be unambiguous in a lookup, short enough to be quotable in a
# support ticket without revealing anything useful.
PREFIX_LENGTH = 12
# 43 base62 characters is ~256 bits — the entropy that makes a fast hash safe
# here, in place of the work factor a password hash would supply.
SECRET_LENGTH = 43

ENVIRONMENT_TAGS = {"dev": "test", "prod": "live"}


@dataclass(frozen=True)
class GeneratedKey:
    """The one and only time the raw key is representable."""

    raw: str
    prefix: str
    key_hash: bytes

    def __repr__(self) -> str:
        # Defensive: a dataclass repr in a traceback or a debug log would
        # otherwise print the raw key. This class is deliberately hard to leak.
        return f"GeneratedKey(prefix={self.prefix!r}, raw='<redacted>')"

    __str__ = __repr__


def _token(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def secret_entropy_bits() -> float:
    return SECRET_LENGTH * _BITS_PER_CHAR


def _hash(raw_secret: str, pepper: str) -> bytes:
    return hmac.new(pepper.encode("utf-8"), raw_secret.encode("utf-8"), sha256).digest()


def generate_key(*, environment: str, pepper: str) -> GeneratedKey:
    """Mint a key. The caller must show ``raw`` once and then discard it."""
    tag = ENVIRONMENT_TAGS.get(environment)
    if tag is None:
        raise ValueError(f"unknown environment: {environment!r}")

    prefix = _token(PREFIX_LENGTH)
    secret = _token(SECRET_LENGTH)
    # The environment is in the key itself so that a key pasted into the wrong
    # config is obvious on sight, before it silently writes test traffic into
    # production reporting.
    raw = f"mmp_{tag}_{prefix}_{secret}"
    return GeneratedKey(raw=raw, prefix=prefix, key_hash=_hash(secret, pepper))


@dataclass(frozen=True)
class ParsedKey:
    environment: str
    prefix: str
    secret: str


def parse_key(raw: str) -> ParsedKey | None:
    """Split a presented key without touching the database.

    Returns None rather than raising: a malformed key is an ordinary
    authentication failure on a public endpoint, not an exceptional condition
    worth an exception's cost or a stack trace in the logs.
    """
    parts = raw.split("_")
    if len(parts) != 4 or parts[0] != "mmp":
        return None
    if len(parts[2]) != PREFIX_LENGTH or len(parts[3]) != SECRET_LENGTH:
        return None
    _, tag, prefix, secret = parts
    environment = next((env for env, t in ENVIRONMENT_TAGS.items() if t == tag), None)
    if environment is None or not prefix or not secret:
        return None
    return ParsedKey(environment=environment, prefix=prefix, secret=secret)


def verify_key(*, presented_secret: str, stored_hash: bytes, pepper: str) -> bool:
    """Constant-time comparison. ``==`` here leaks the hash a byte at a time."""
    return hmac.compare_digest(_hash(presented_secret, pepper), stored_hash)


API_KEY_CACHE_PREFIX = "apikey:"


def api_key_cache_key(prefix: str) -> str:
    """Where the tracker caches an authenticated key record.

    Here rather than in either service because both need it and they must
    agree: the tracker writes these entries, and the API deletes them when a key
    is revoked or rotated, or when an app is disabled. A revocation the cache
    has not been told about is not a revocation.

    It was a constant in the tracker and three separate literals in the API,
    which agreed by inspection and nothing more.
    """
    return f"{API_KEY_CACHE_PREFIX}{prefix}"
