"""The outbound client.

Postback rules and webhooks are user-supplied URLs that our servers fetch. This
is the sharpest attack surface in the platform: unguarded, anyone who can create
a rule has a request forwarder inside the VPC, pointed wherever they like — and
in particular at 169.254.169.254, which on a misconfigured instance hands out
credentials.
"""

from __future__ import annotations

import contextlib
import re
import socket

import pytest
from mmp_core.outbound import (
    BLOCKED_NETWORKS,
    BlockedDestination,
    fetch,
    is_permitted,
    validate_destination,
)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://169.254.169.254/latest/meta-data/", "cloud metadata — credentials"),
        ("https://169.254.170.2/v2/credentials", "ECS task metadata"),
        ("https://127.0.0.1:8002/v1/apps", "our own API"),
        ("https://localhost/admin", "loopback by name"),
        ("https://10.0.0.5/internal", "RFC 1918"),
        ("https://172.16.0.1/internal", "RFC 1918"),
        ("https://192.168.1.1/router", "RFC 1918"),
        ("https://0.0.0.0/", "this host"),
        ("https://[::1]/", "IPv6 loopback"),
        ("https://[fe80::1]/", "IPv6 link-local"),
        ("https://[fc00::1]/", "IPv6 unique local"),
        ("https://100.64.0.1/", "carrier-grade NAT"),
        ("https://224.0.0.1/", "multicast"),
    ],
)
def test_internal_destinations_are_blocked(url, why):
    with pytest.raises(BlockedDestination):
        validate_destination(url)
    assert not is_permitted(url)[0], why


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/postback",
        "file:///etc/passwd",
        "gopher://evil.example/",
        "ftp://example.com/",
        "//example.com/protocol-relative",
    ],
)
def test_only_https_is_permitted(url):
    """An http destination would let anyone on the path rewrite a conversion
    postback, and the non-http schemes are pure SSRF primitives."""
    with pytest.raises(BlockedDestination):
        validate_destination(url)


def test_public_destinations_are_allowed():
    target = validate_destination("https://example.com/postback")
    assert target.hostname == "example.com"
    assert target.port == 443


def test_every_resolved_address_is_checked(monkeypatch):
    """A hostname can resolve to several addresses.

    Checking only the first would let a name that returns one public and one
    private address through, after which the OS decides which one we connect to.
    """

    def multi_homed(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", multi_homed)
    with pytest.raises(BlockedDestination, match=re.escape("169.254.169.254")):
        validate_destination("https://mixed.example/")


def test_unresolvable_hostname_is_blocked():
    with pytest.raises(BlockedDestination):
        validate_destination("https://this-name-does-not-resolve.invalid/")


def test_blocked_networks_cover_the_ranges_that_matter():
    """A regression guard on the list itself.

    Someone trimming this list to 'simplify' it is exactly how the metadata
    endpoint becomes reachable again.
    """
    import ipaddress

    must_block = [
        "169.254.169.254",  # cloud metadata
        "127.0.0.1",
        "10.1.2.3",
        "172.20.0.1",
        "192.168.0.1",
        "::1",
        "fe80::1",
    ]
    for address in must_block:
        parsed = ipaddress.ip_address(address)
        assert any(parsed.version == net.version and parsed in net for net in BLOCKED_NETWORKS), (
            f"{address} is not covered"
        )


async def test_dns_rebinding_cannot_change_the_destination(monkeypatch):
    """The bypass a hostname allowlist does not close.

    An attacker's domain resolves to a public address when we validate it, then
    to the metadata endpoint when the HTTP client resolves it again. The fix is
    to connect to the address we already validated, so there is no second
    lookup to poison — and this test proves the request actually goes there.
    """
    from mmp_core import outbound

    validated = outbound.validate_destination("https://example.com/")

    seen: dict[str, str] = {}

    class RecordingTransport(outbound._PinnedTransport):
        async def handle_async_request(self, request):
            # Capture what the pinning rewrote before any connection is made.
            original_host = request.url.host
            # The connection may fail or succeed; either way, what matters is
            # what the pinning rewrote before it was attempted.
            with contextlib.suppress(Exception):
                await super().handle_async_request(request)
            seen["connect_host"] = request.url.host
            seen["host_header"] = request.headers.get("host", "")
            seen["sni"] = request.extensions.get("sni_hostname", "")
            seen["original"] = original_host
            raise RuntimeError("stopped before the network")

    monkeypatch.setattr(outbound, "_PinnedTransport", RecordingTransport)
    with pytest.raises(RuntimeError):
        await outbound.fetch("https://example.com/")

    assert seen["connect_host"] == validated.address, (
        "the connection must go to the validated IP, not to a re-resolved name"
    )
    assert seen["host_header"] == "example.com", "the Host header must survive"
    assert seen["sni"] == "example.com", "TLS validation must still use the hostname"


async def test_redirects_are_not_followed():
    """A 302 to an internal address would walk past every check above."""
    import inspect as inspect_module

    source = inspect_module.getsource(fetch)
    assert "follow_redirects=False" in source


@pytest.mark.parametrize(
    ("host", "allowlist", "expected"),
    [
        ("partner.example", ("partner.example",), True),
        ("eu.partner.example", ("partner.example",), True),
        ("a.b.partner.example", ("partner.example",), True),
        ("PARTNER.EXAMPLE", ("partner.example",), True),
        ("partner.example.", ("partner.example",), True),  # trailing root dot
        # The bypass a suffix check without the dot would let through.
        ("evilpartner.example", ("partner.example",), False),
        ("partner.example.evil.com", ("partner.example",), False),
        ("other.example", ("partner.example",), False),
        ("partner.example", (), False),
    ],
)
def test_allowlist_matching_is_exact_about_subdomains(host, allowlist, expected):
    """Pure logic, tested without DNS.

    `evilpartner.example` must not match an entry of `partner.example` — a
    suffix check without the leading dot is a classic and entirely practical
    bypass.
    """
    from mmp_core.outbound import host_in_allowlist

    assert host_in_allowlist(host, allowlist) is expected


def test_allowlist_applies_on_top_of_the_network_checks():
    """The allowlist narrows; it never widens.

    An organisation cannot allowlist its way to the metadata endpoint.
    """
    allowed, _ = is_permitted("https://example.com/cb", allowlist=("example.com",))
    assert allowed

    blocked, reason = is_permitted("https://127.0.0.1/cb", allowlist=("127.0.0.1", "localhost"))
    assert not blocked
    assert "127.0.0.0/8" in reason, "the network check must run first"
