"""One-way transforms for identifiers we must correlate but must not retain.

An IP address is personal data in the EU and a device advertising ID is personal
data nearly everywhere. Attribution needs to know whether the address at click
time matches the address at install time; it never needs to know the address.

So: HMAC under a pepper that **rotates daily**. Matching still works within a
day, which is all any attribution window needs from an IP. Correlation across
days is cryptographically unavailable — including to us, once the old pepper
ages out. Rotation is the difference between a hashed identifier and a
pseudonymous one that is trivially re-identified with a rainbow table over the
4 billion IPv4 addresses.
"""

from __future__ import annotations

import datetime as dt
import hmac
import ipaddress
from hashlib import sha256

# Truncated to 128 bits. Full SHA-256 is more collision resistance than a
# same-day match needs, and a shorter value is a smaller thing to store on every
# one of hundreds of millions of rows.
DIGEST_BYTES = 16


def _daily_pepper(pepper: str, day: dt.date) -> bytes:
    return f"{pepper}:{day.isoformat()}".encode()


def hash_ip(ip: str, *, pepper: str, day: dt.date | None = None) -> bytes | None:
    """Hash an IP for same-day correlation. Returns None if it is not an IP.

    The address is normalised first so that ``::ffff:1.2.3.4`` and ``1.2.3.4``
    produce the same digest — otherwise an IPv6-mapped client silently fails to
    match its own click.
    """
    try:
        normalised = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(normalised, ipaddress.IPv6Address) and normalised.ipv4_mapped:
        normalised = normalised.ipv4_mapped

    day = day or dt.datetime.now(dt.UTC).date()
    return hmac.new(_daily_pepper(pepper, day), str(normalised).encode("utf-8"), sha256).digest()[
        :DIGEST_BYTES
    ]


def hash_device_id(device_id: str, *, pepper: str) -> bytes | None:
    """Hash a GAID/IDFA.

    Not day-rotated: a device match is the whole basis of deterministic
    attribution across an install window that can be 30 days long. The
    protection here is retention — these rows are dropped when the window
    closes — not rotation.
    """
    cleaned = device_id.strip().lower()
    # All-zero advertising IDs are what Android returns for a user who has opted
    # out. Hashing it would create a bucket that matches every opted-out device
    # on the platform to every other one.
    if not cleaned or set(cleaned) <= {"0", "-"}:
        return None
    return hmac.new(pepper.encode("utf-8"), cleaned.encode("utf-8"), sha256).digest()[:DIGEST_BYTES]


def truncate_ip_for_geo(ip: str) -> str | None:
    """Coarsen an address to a /24 (or /48 for IPv6) for geo lookup.

    Country resolution does not need host precision, and passing a full address
    to a geo provider is an unnecessary disclosure to a third party.
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return str(ipaddress.ip_network(f"{address}/24", strict=False).network_address)
    return str(ipaddress.ip_network(f"{address}/48", strict=False).network_address)
