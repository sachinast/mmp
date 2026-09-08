"""The SSRF-safe HTTP client.

Postback rules and webhooks are, by design, **user-supplied URLs that our
servers fetch**. That is the single sharpest piece of attack surface in this
platform: unguarded, anyone who can create a postback rule has a request
forwarder inside our VPC, pointed at whatever they like — internal admin
endpoints, a database's HTTP interface, and above all the cloud metadata service
at 169.254.169.254, which on a misconfigured instance hands out credentials.

A hostname allowlist alone does not close this. The classic bypass is DNS
rebinding: the attacker's domain resolves to a public address when we validate
it and to 169.254.169.254 a moment later when the HTTP client resolves it again.
Two lookups, two answers, and the check was performed on the wrong one.

So the sequence here is:

1. Parse and require https.
2. Resolve the hostname **ourselves**, once.
3. Reject every address that is private, loopback, link-local, multicast,
   reserved, or in the metadata range — checking *all* returned addresses, since
   a name can resolve to several.
4. Connect to the **validated IP**, carrying the original ``Host`` header and
   the correct TLS server name. There is no second lookup, so there is no window
   to rebind in.
5. Refuse redirects entirely. A 302 to ``http://169.254.169.254`` would
   otherwise walk straight past every check above.

This is defence in depth, not the only defence: outbound delivery also runs from
a network segment with no route to internal services. But that control lives in
infrastructure someone else owns, and this one lives here.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from mmp_core.logging import get_logger

log = get_logger(__name__)

# Everything a public integration has no business reaching. Checked as networks
# rather than as strings, so no amount of encoding trickery in the hostname
# changes the answer — by this point we are looking at a resolved address.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this host"
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local — includes cloud metadata
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.168.0.0/16",  # RFC 1918
        "198.18.0.0/15",  # benchmarking
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "::1/128",  # IPv6 loopback
        "fc00::/7",  # IPv6 unique local
        "fe80::/10",  # IPv6 link-local
        "ff00::/8",  # IPv6 multicast
        "::/128",  # unspecified
        "64:ff9b::/96",  # NAT64 — can wrap a private IPv4
    )
)

# A response body from a partner is logged for the delivery record and read no
# further. Without a cap, a hostile endpoint could stream gigabytes at a worker.
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


class BlockedDestination(Exception):
    """The destination is not somewhere this platform will send a request."""


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    hostname: str
    address: str
    port: int


@dataclass
class OutboundResponse:
    status_code: int
    body: str
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)


def _addresses_for(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedDestination(f"could not resolve {hostname}") from exc
    # info[4] is the sockaddr; its first element is the address for both
    # AF_INET and AF_INET6, but the tuple shape differs, hence the cast.
    return sorted({str(info[4][0]) for info in infos})


def validate_destination(url: str, *, allow_http: bool = False) -> ResolvedTarget:
    """Resolve and vet a destination. Raises rather than returning a verdict.

    ``allow_http`` exists only for the sandbox endpoint, which is ours and
    internal. It is never set from configuration a customer can reach.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", *(("http",) if allow_http else ())):
        raise BlockedDestination(f"scheme {parsed.scheme!r} is not permitted")
    if not parsed.hostname:
        raise BlockedDestination("no hostname in URL")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _addresses_for(parsed.hostname, port)
    if not addresses:
        raise BlockedDestination(f"{parsed.hostname} resolved to nothing")

    # Every address, not just the first. A name that resolves to one public and
    # one private address would otherwise pass validation and then connect to
    # whichever the OS preferred.
    for candidate in addresses:
        address = ipaddress.ip_address(candidate)
        for network in BLOCKED_NETWORKS:
            if address.version == network.version and address in network:
                raise BlockedDestination(
                    f"{parsed.hostname} resolves to {candidate}, which is in {network}"
                )

    return ResolvedTarget(url=url, hostname=parsed.hostname, address=addresses[0], port=port)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """Connect to the address we validated, and to no other.

    This is what actually closes the DNS rebinding window. Validating a hostname
    and then handing the same hostname to the HTTP client means two lookups —
    and an attacker only has to make the second one answer differently. So the
    request's URL host is rewritten to the validated IP before it reaches the
    connection pool, while the ``Host`` header and the TLS server name stay the
    original hostname so virtual hosting and certificate validation still work.

    Without this rewrite the rest of the module is a comment, not a control.
    """

    def __init__(self, target: ResolvedTarget) -> None:
        super().__init__(retries=0)
        self._target = target

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        # An IPv6 literal needs brackets in a URL authority.
        literal = (
            f"[{self._target.address}]" if ":" in self._target.address else self._target.address
        )
        request.url = request.url.copy_with(host=literal, port=self._target.port)
        request.headers["host"] = (
            original_host
            if self._target.port in (80, 443)
            else f"{original_host}:{self._target.port}"
        )
        request.extensions = dict(request.extensions or {})
        request.extensions["sni_hostname"] = original_host
        return await super().handle_async_request(request)


async def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    request_timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    allow_http: bool = False,
) -> OutboundResponse:
    """Make one outbound request to a vetted destination.

    Never follows redirects. A 302 to an internal address would walk past every
    check above, and no legitimate postback endpoint needs one.
    """
    import time

    target = validate_destination(url, allow_http=allow_http)
    started = time.perf_counter()

    async with httpx.AsyncClient(
        timeout=request_timeout,
        follow_redirects=False,
        transport=_PinnedTransport(target),
        # Connections are not reused between destinations: a pooled connection
        # is pinned to one address, and reusing it for a different hostname
        # would defeat the validation for the second one.
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        response = await client.request(method, url, headers=headers or {}, content=content)
        body = response.text[:MAX_RESPONSE_BYTES]

    return OutboundResponse(
        status_code=response.status_code,
        body=body,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def host_in_allowlist(hostname: str, allowlist: tuple[str, ...]) -> bool:
    """Whether a hostname is covered by an allowlist entry.

    Pure, and separated from resolution so it can be tested without DNS. An
    entry matches the host itself or any subdomain of it — and the leading dot
    matters: without it, an entry of ``partner.example`` would also match
    ``evilpartner.example``, which is a classic and entirely practical bypass.
    """
    host = hostname.lower().rstrip(".")
    return any(
        host == entry.lower() or host.endswith(f".{entry.lower().rstrip('.')}")
        for entry in allowlist
    )


def is_permitted(url: str, *, allowlist: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Check a destination without sending anything.

    Used when a rule is saved, so a bad URL is a validation error the user sees
    immediately rather than a delivery failure they find in a log days later.
    """
    try:
        target = validate_destination(url)
    except BlockedDestination as exc:
        return False, str(exc)

    if allowlist:
        host = target.hostname.lower()
        permitted = any(
            host == entry.lower() or host.endswith(f".{entry.lower()}") for entry in allowlist
        )
        if not permitted:
            return False, f"{target.hostname} is not in this organisation's allowlist"
    return True, ""
