"""GET /c/{tracking_code} — the click redirect.

This is the most latency-sensitive path in the platform, and the reason is
commercial rather than technical: the person on the other end is waiting to
reach an app store, and every hundred milliseconds spent here is a share of them
who leave instead. It is also the only endpoint whose slowness is visible to an
advertiser's customers rather than to the advertiser.

So the handler does a fixed, tiny amount of work and nothing that can block:

1. Dict lookup in the in-process link cache. No database.
2. Mint a UUIDv7 click_id.
3. Classify the user agent by substring — no parsing library.
4. Hash the IP. The raw address is never stored.
5. Append to the shipping buffer. Not an await on Redis.
6. 302.

Everything else — writing the click, attribution, fraud scoring — happens after
the person is already at the store.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import quote, urlencode, urlparse, urlunparse

from mmp_core.ids import uuid7
from mmp_core.logging import get_logger
from mmp_crypto.pii import hash_device_id, hash_ip
from mmp_ingest.clicks import MAX_SUB_PARAM, MAX_USER_AGENT, QueuedClick
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from mmp_tracker.state import TrackerState
from mmp_tracker.useragent import Platform, classify, os_version

log = get_logger(__name__)

# Google Play reads this parameter on install and hands it back through the
# Install Referrer API. It is how a click on Android becomes an attributed
# install with certainty rather than inference — the single highest-fidelity
# signal available on any platform, and the reason Android attribution is
# deterministic while iOS is not.
PLAY_REFERRER_PARAM = "referrer"
CLICK_ID_PARAM = "mmp_click_id"

# Cache-Control on a redirect matters more than it looks. Without it an
# intermediary may cache the 302, and every subsequent click through that proxy
# reuses one click_id — silently collapsing many clicks into one and destroying
# the attribution for all but the first.
NO_STORE = {
    "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
    "pragma": "no-cache",
}


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _with_query(url: str, params: dict[str, str]) -> str:
    """Append parameters, preserving whatever the destination already had."""
    parsed = urlparse(url)
    existing = parsed.query
    addition = urlencode(params)
    query = f"{existing}&{addition}" if existing else addition
    return urlunparse(parsed._replace(query=query))


def build_destination(
    *,
    platform: Platform,
    android_url: str | None,
    ios_url: str | None,
    fallback_url: str,
    click_id: str,
    deep_link_path: str | None,
) -> str:
    """Choose the destination and thread the click_id into it.

    Android gets the click id inside the Play Store ``referrer`` parameter,
    which survives the install and comes back through the Install Referrer API.

    iOS gets it as an ordinary query parameter, which the App Store does **not**
    forward. That is not an oversight to be fixed later: Apple provides no
    equivalent channel, which is why iOS attribution needs SKAdNetwork and a
    deferred deep-link handshake rather than a referrer. The parameter is still
    attached because it is useful on the web fallback path.
    """
    if platform is Platform.ANDROID and android_url:
        referrer = f"utm_content={quote(click_id)}"
        if deep_link_path:
            referrer += f"&deep_link={quote(deep_link_path, safe='')}"
        return _with_query(android_url, {PLAY_REFERRER_PARAM: referrer})

    if platform is Platform.IOS and ios_url:
        return _with_query(ios_url, {CLICK_ID_PARAM: click_id})

    return _with_query(fallback_url, {CLICK_ID_PARAM: click_id})


async def redirect_click(request: Request) -> Response:
    state: TrackerState = request.app.state.tracker
    tracking_code = request.path_params["tracking_code"]
    now = dt.datetime.now(dt.UTC)

    link = state.links.get(tracking_code)
    if link is None:
        if state.links.is_known_missing(tracking_code, now=now):
            state.unknown_codes += 1
            return PlainTextResponse("not found", status_code=404, headers=NO_STORE)
        # One indexed lookup, then cached. Reached for a link created moments
        # ago, or by a process that started before it existed.
        link = await state.links.load_missing(tracking_code)
        if link is None:
            state.unknown_codes += 1
            return PlainTextResponse("not found", status_code=404, headers=NO_STORE)

    click_id = str(uuid7())
    user_agent = request.headers.get("user-agent")
    platform, is_bot = classify(user_agent)

    destination = build_destination(
        platform=platform,
        android_url=link.android_url,
        ios_url=link.ios_url,
        fallback_url=link.fallback_url,
        click_id=click_id,
        deep_link_path=link.deep_link_path,
    )

    params = request.query_params
    ip = _client_ip(request)
    # Networks sometimes pass the advertising ID on the click. When they do,
    # attribution becomes deterministic on Android without waiting for the
    # referrer, so it is worth capturing — hashed, never stored raw.
    raw_device_id = params.get("gaid") or params.get("idfa") or params.get("device_id")

    click = QueuedClick(
        click_id=click_id,
        clicked_at=now.isoformat(),
        organization_id=link.organization_id,
        app_id=link.app_id,
        campaign_id=link.campaign_id,
        tracking_link_id=link.id,
        device_hash=(
            hash_device_id(raw_device_id, pepper=state.settings.ip_hash_pepper)
            if raw_device_id
            else None
        ),
        ip_hash=hash_ip(ip, pepper=state.settings.ip_hash_pepper) if ip else None,
        country=None,  # geo resolution lands with the enrichment worker
        platform=int(platform),
        os_version=os_version(user_agent, platform),
        device_model=None,
        user_agent=user_agent[:MAX_USER_AGENT] if user_agent else None,
        sub1=(params.get("sub1") or None) and params["sub1"][:MAX_SUB_PARAM],
        sub2=(params.get("sub2") or None) and params["sub2"][:MAX_SUB_PARAM],
        sub3=(params.get("sub3") or None) and params["sub3"][:MAX_SUB_PARAM],
        # Flagged, never blocked. A false positive that blocks is a lost
        # conversion for a real user; a false positive that flags is a row in a
        # fraud report someone can review.
        is_bot=is_bot,
    )

    dropped = state.click_buffer.append([click])
    if not dropped:
        state.clicks_total += 1

    # 302, not 301: a permanent redirect would be cached by the browser and by
    # every intermediary, and subsequent clicks would never reach us at all.
    return RedirectResponse(destination, status_code=302, headers=NO_STORE)
