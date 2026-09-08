"""HMAC request signing, for server-to-server calls in both directions.

Used for two things that look similar and are not:

* **Inbound S2S events.** An advertiser's backend posts conversions to us. A
  bearer credential alone would be enough to authenticate, but it would not stop
  someone who captured one request from replaying it — and a replayed purchase
  is a duplicate conversion sent to an ad network, which is money.
* **Outbound webhooks.** We post to a customer's endpoint, and they need to know
  the request is genuinely ours and has not been modified in transit.

The signature covers a **canonical request**: method, path, timestamp, and a
digest of the body. Signing only the body would let an attacker replay the same
payload against a different endpoint; signing only a timestamp would let them
change the payload. Both have to be in the string that gets signed.

Replay protection is two-part and both parts are needed. The timestamp bounds
how long a captured request stays useful; the nonce cache makes it useless
immediately within that window. A timestamp alone permits replay for its whole
tolerance, and a nonce cache alone would have to be unbounded.
"""

from __future__ import annotations

import datetime as dt
import hmac
from dataclasses import dataclass
from hashlib import sha256

SIGNATURE_HEADER = "x-mmp-signature"
TIMESTAMP_HEADER = "x-mmp-timestamp"
KEY_ID_HEADER = "x-mmp-key-id"

# How far a request's timestamp may be from ours. Wide enough for ordinary clock
# drift on a customer's server, narrow enough that a captured request stops
# being useful quickly.
DEFAULT_TOLERANCE = dt.timedelta(minutes=5)

# The version prefix is not decoration. When the scheme changes — a different
# hash, a different canonical form — both must be accepted during the migration,
# and an unversioned signature gives no way to tell them apart.
SCHEME = "v1"


class SignatureError(Exception):
    """The request is not acceptably signed. Never says which part failed."""


@dataclass(frozen=True)
class SignedRequest:
    timestamp: str
    signature: str

    def headers(self, key_id: str | None = None) -> dict[str, str]:
        headers = {TIMESTAMP_HEADER: self.timestamp, SIGNATURE_HEADER: self.signature}
        if key_id:
            headers[KEY_ID_HEADER] = key_id
        return headers


def canonical_request(*, method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """The exact bytes that get signed.

    Newline-delimited with a fixed field order. A separator that can appear
    inside a field would let two different requests produce the same canonical
    form — the classic length-extension-by-ambiguity mistake — so the path is
    included whole and the body only as a digest.
    """
    body_digest = sha256(body).hexdigest()
    return "\n".join([SCHEME, method.upper(), path, timestamp, body_digest]).encode()


def sign(
    *, method: str, path: str, body: bytes, secret: str, now: dt.datetime | None = None
) -> SignedRequest:
    timestamp = str(int((now or dt.datetime.now(dt.UTC)).timestamp()))
    message = canonical_request(method=method, path=path, timestamp=timestamp, body=body)
    digest = hmac.new(secret.encode("utf-8"), message, sha256).hexdigest()
    return SignedRequest(timestamp=timestamp, signature=f"{SCHEME}={digest}")


def verify(
    *,
    method: str,
    path: str,
    body: bytes,
    secret: str,
    signature: str,
    timestamp: str,
    tolerance: dt.timedelta = DEFAULT_TOLERANCE,
    now: dt.datetime | None = None,
) -> None:
    """Raise ``SignatureError`` unless the request is validly signed and fresh.

    Deliberately raises the same exception for every failure. Distinguishing
    "bad signature" from "stale timestamp" tells an attacker which half of their
    forgery to work on.
    """
    try:
        sent_at = dt.datetime.fromtimestamp(int(timestamp), tz=dt.UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise SignatureError("invalid signature") from exc

    reference = now or dt.datetime.now(dt.UTC)
    # Absolute, so a clock that is ahead is rejected as well as one behind. A
    # far-future timestamp would otherwise stay valid indefinitely.
    if abs(reference - sent_at) > tolerance:
        raise SignatureError("invalid signature")

    expected = hmac.new(
        secret.encode("utf-8"),
        canonical_request(method=method, path=path, timestamp=timestamp, body=body),
        sha256,
    ).hexdigest()

    presented = signature.removeprefix(f"{SCHEME}=")
    # compare_digest, never ==. A byte-by-byte comparison leaks the correct
    # signature one character at a time to anyone who can measure the response.
    if not hmac.compare_digest(expected, presented):
        raise SignatureError("invalid signature")


def nonce_for(*, signature: str) -> str:
    """The replay-cache key for a request.

    The signature itself: it is unique per (secret, method, path, timestamp,
    body), which is exactly the tuple a replay would have to reproduce.
    """
    return sha256(signature.encode("utf-8")).hexdigest()[:32]
