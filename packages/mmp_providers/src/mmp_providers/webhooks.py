"""Outgoing webhooks.

Postbacks and webhooks look similar and answer to different people. A postback
is shaped by an **ad network**: their URL, their macros, their success codes,
and we bend to it. A webhook is shaped by **us**: one payload format, one
signature scheme, documented once, and the customer's endpoint accommodates it.

That difference is why they are separate modules rather than one parameterised
one. Trying to make a single delivery path serve both means every change for a
network's quirk risks the contract we publish to customers.

Two things a webhook must provide that a postback need not:

* **A signature the receiver can verify.** They are accepting state changes from
  us over the public internet; without a signature, anyone who learns the URL
  can post fabricated conversions into their systems.
* **A replay-resistant timestamp.** Same reasoning as inbound S2S, in the other
  direction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import msgspec
from mmp_core.logging import get_logger
from mmp_crypto.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

log = get_logger(__name__)

# The events a customer may subscribe to. A closed set, because each one is a
# published contract: adding one is a documentation change, and removing one
# breaks an integration.
WEBHOOK_EVENTS: frozenset[str] = frozenset(
    {
        "install",
        "signup",
        "purchase",
        "subscription_start",
        "refund",
        "attribution_created",
    }
)

# After this many consecutive failures the webhook is disabled.
#
# Not a courtesy to us — a courtesy to them. An endpoint that has been returning
# 500s for a day is not coming back on its own, and continuing to retry into it
# generates load on a system that is already broken and noise in logs someone
# will eventually have to read.
MAX_CONSECUTIVE_FAILURES = 20

_encoder = msgspec.json.Encoder()


@dataclass(frozen=True)
class WebhookPayload:
    """The published envelope. Its shape is a contract; changing it is a
    breaking API change under the deprecation policy in docs/API_VERSIONING.md.
    """

    id: str
    type: str
    created_at: str
    data: dict[str, Any]

    def encode(self) -> bytes:
        return _encoder.encode(
            {
                "id": self.id,
                "type": self.type,
                "created_at": self.created_at,
                "data": self.data,
            }
        )


def build_payload(
    *, event_type: str, data: dict[str, Any], delivery_id: uuid.UUID | None = None
) -> WebhookPayload:
    from mmp_core.ids import uuid7

    return WebhookPayload(
        id=str(delivery_id or uuid7()),
        type=event_type,
        created_at=dt.datetime.now(dt.UTC).isoformat(),
        data=data,
    )


def signed_headers(
    *, payload: bytes, secret: str, url_path: str, delivery_id: str
) -> dict[str, str]:
    """Headers a receiver can verify.

    The signature covers the same canonical form as inbound S2S — method, path,
    timestamp, body digest — so a customer implementing verification can follow
    one description for both directions rather than two nearly-identical ones.
    """
    signature = sign(method="POST", path=url_path, body=payload, secret=secret)
    return {
        "content-type": "application/json",
        "user-agent": "mmp-webhooks/1",
        # Echoed so a receiver can deduplicate on their side. They will see a
        # repeat eventually — at-least-once is the only thing an outbound
        # delivery system can honestly promise.
        "x-mmp-delivery-id": delivery_id,
        SIGNATURE_HEADER: signature.signature,
        TIMESTAMP_HEADER: signature.timestamp,
    }


def should_disable(consecutive_failures: int) -> bool:
    return consecutive_failures >= MAX_CONSECUTIVE_FAILURES


def verification_snippet(
    secret_hint: str = "<your signing secret>",  # noqa: S107 # nosec B107 - a placeholder
) -> str:
    """The verification code we hand a customer.

    Kept next to the signing code so the two cannot drift. A webhook signature
    nobody can verify is a webhook signature that does nothing, and the usual
    reason is documentation that fell behind the implementation.
    """
    return f"""# Verify an MMP webhook (Python)
import hmac, time
from hashlib import sha256

SECRET = {secret_hint!r}
TOLERANCE_SECONDS = 300

def verify(request_path: str, body: bytes, headers: dict) -> bool:
    timestamp = headers["x-mmp-timestamp"]
    if abs(time.time() - int(timestamp)) > TOLERANCE_SECONDS:
        return False  # too old to accept; likely a replay
    canonical = "\\n".join(
        ["v1", "POST", request_path, timestamp, sha256(body).hexdigest()]
    ).encode()
    expected = hmac.new(SECRET.encode(), canonical, sha256).hexdigest()
    presented = headers["x-mmp-signature"].removeprefix("v1=")
    return hmac.compare_digest(expected, presented)  # never ==
"""
