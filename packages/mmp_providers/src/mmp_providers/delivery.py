"""Outbound delivery: postbacks to ad networks, webhooks to customers.

The failure that matters here is not a dropped delivery — it is a **duplicated**
one. A postback tells an ad network that a conversion happened, and networks
optimise spend on those signals. Sending one twice inflates a campaign's
apparent performance, corrupts the network's model, and in a cost-per-action
arrangement means paying twice for one action.

So delivery is claimed before it is attempted. A row in ``postback_deliveries``
with ``UNIQUE (postback_rule_id, event_id)`` is inserted first; whoever inserts
it owns the send. A redelivered queue message finds the row already there and
does nothing. That the claim and the send cannot be one atomic operation is the
irreducible part — the network call can succeed and the status update can fail —
so the design biases toward *not* re-sending: a delivery whose outcome we are
unsure of is marked failed and retried only through an explicit, bounded path.

Retries are exponential with jitter. Constant-interval retries from many workers
synchronise into a thundering herd against a partner who is already struggling,
which turns their brownout into their outage and our support ticket.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from mmp_core.logging import get_logger
from mmp_core.outbound import BlockedDestination, OutboundResponse, fetch
from mmp_db.types import DbConn

log = get_logger(__name__)

MAX_ATTEMPTS = 5
# Roughly 30s, 2m, 8m, 30m. Long enough that a partner's short outage is ridden
# out; short enough that a conversion is not reported a day late.
BACKOFF_BASE = dt.timedelta(seconds=30)
BACKOFF_FACTOR = 4
MAX_BACKOFF = dt.timedelta(hours=1)

# Stored so a support engineer can see what the partner said. Truncated because
# an unbounded partner response is unbounded write amplification on the busiest
# outbound table we have.
MAX_STORED_BODY = 2000


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    status_code: int | None
    body: str | None
    error: str | None
    elapsed_ms: int | None = None

    @property
    def retryable(self) -> bool:
        """Whether trying again could plausibly work.

        A 4xx means the partner understood us and refused; sending the identical
        request again will be refused identically, and retrying it just burns
        their rate limit and our workers. A 429 is the exception — it is
        explicitly "try again" — and 408 is a timeout wearing a 4xx.
        """
        if self.error and self.error.startswith("blocked:"):
            # A destination we refuse to reach. Retrying re-runs the same check
            # and gets the same answer — it is a permanent verdict, not a
            # transient failure, even though no status code came back.
            return False
        if self.status_code is None:
            return True  # never reached them; the network may recover
        if self.status_code in (408, 429):
            return True
        return self.status_code >= 500


def next_retry_at(attempt: int, *, now: dt.datetime | None = None) -> dt.datetime:
    """When to try again, with jitter.

    The jitter is not decoration. Without it, every worker that failed in the
    same outage retries at the same instant, and a partner recovering from a
    brownout is hit by the whole backlog at once — turning their recovery into a
    second outage.
    """
    now = now or dt.datetime.now(dt.UTC)
    delay = min(BACKOFF_BASE * (BACKOFF_FACTOR ** max(attempt - 1, 0)), MAX_BACKOFF)
    # random, not secrets: this spreads retry times so workers do not
    # synchronise, which is a scheduling concern rather than a security one.
    # An attacker predicting when we retry a postback gains nothing.
    jittered = delay.total_seconds() * random.uniform(0.5, 1.5)  # noqa: S311 # nosec B311
    return now + dt.timedelta(seconds=jittered)


CLAIM_SQL = """
INSERT INTO postback_deliveries (
    id, organization_id, postback_rule_id, event_id, status, attempt_count, created_at
)
VALUES ($1, $2, $3, $4, 'in_flight', 1, now())
ON CONFLICT (postback_rule_id, event_id) DO NOTHING
RETURNING id
"""

RECORD_SQL = """
UPDATE postback_deliveries
-- $2 is cast explicitly because it appears both as a value and inside a
-- comparison; without the cast Postgres cannot deduce one type for it.
SET status = $2::text,
    response_status = $3,
    response_body = $4,
    error = $5,
    delivered_at = CASE WHEN $2::text = 'delivered' THEN now() ELSE delivered_at END,
    next_retry_at = $6,
    -- Stored so a retry can re-send the exact request. The URL embeds the
    -- event's context, which is gone by the time the backoff elapses: the queue
    -- message was acknowledged when the first attempt was recorded.
    request_url = coalesce($7, request_url)
WHERE id = $1
"""

RETRY_CLAIM_SQL = """
UPDATE postback_deliveries
SET status = 'in_flight', attempt_count = attempt_count + 1
WHERE id = $1 AND status = 'failed'
RETURNING attempt_count
"""


async def claim(
    conn: DbConn,
    *,
    delivery_id: object,
    organization_id: object,
    rule_id: object,
    event_id: object,
) -> bool:
    """Take ownership of one (rule, event) delivery.

    Returns False when someone already has it. This is the whole duplicate
    defence: the unique constraint decides the winner, and every other worker
    walks away.
    """
    claimed = await conn.fetchval(CLAIM_SQL, delivery_id, organization_id, rule_id, event_id)
    return claimed is not None


async def send(
    *,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    allow_http: bool = False,
) -> DeliveryResult:
    """One attempt. Never raises; a failure is a result, not an exception."""
    try:
        response: OutboundResponse = await fetch(
            url, method=method, headers=headers, content=body, allow_http=allow_http
        )
    except BlockedDestination as exc:
        # Not retryable and not the partner's fault: the destination is one we
        # refuse to reach. Retrying would just re-run the same check.
        return DeliveryResult(delivered=False, status_code=None, body=None, error=f"blocked: {exc}")
    except Exception as exc:
        return DeliveryResult(
            delivered=False,
            status_code=None,
            body=None,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )

    return DeliveryResult(
        delivered=False,  # the caller decides, using the rule's success codes
        status_code=response.status_code,
        body=response.body[:MAX_STORED_BODY],
        error=None,
        elapsed_ms=response.elapsed_ms,
    )


async def record(
    conn: DbConn,
    *,
    delivery_id: object,
    result: DeliveryResult,
    success_codes: list[int],
    attempt: int,
    request_url: str | None = None,
    accepted: bool | None = None,
) -> str:
    """Write the outcome and decide what happens next.

    ``accepted`` is the adapter's reading of the response, and it wins over the
    status code when supplied. Without it the adapter's ``interpret`` would be
    decorative: a provider returning 200 with an error in the body would be
    recorded as delivered because 200 is in the success list, which is exactly
    the case that method exists to catch.
    """
    delivered = accepted if accepted is not None else result.status_code in success_codes

    if delivered:
        status = "delivered"
        retry_at = None
    elif not result.retryable:
        # A definitive refusal. Retrying an identical request that a partner
        # already understood and rejected only burns their rate limit.
        status = "abandoned"
        retry_at = None
    elif attempt >= MAX_ATTEMPTS:
        status = "abandoned"
        retry_at = None
    else:
        status = "failed"
        retry_at = next_retry_at(attempt)

    await conn.execute(
        RECORD_SQL,
        delivery_id,
        status,
        result.status_code,
        (result.body or "")[:MAX_STORED_BODY] or None,
        result.error,
        retry_at,
        request_url,
    )
    return status
