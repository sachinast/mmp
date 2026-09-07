"""UUIDv7 — time-ordered identifiers.

Every high-volume primary key in this system is a v7 rather than a v4. Two
reasons, both load-bearing:

* **Index locality.** v4 values scatter uniformly across the btree, so every
  insert dirties a different page. v7 values are monotonic in their first 48
  bits, so inserts append to the right-hand edge of the index.
* **A free timestamp.** ``timestamp_ms()`` recovers when an ID was minted, which
  gives the fraud rules a click-to-install delta without a join.

RFC 9562 layout: 48-bit big-endian Unix milliseconds, 4-bit version, 12 bits of
randomness, 2-bit variant, 62 bits of randomness.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7(*, ms: int | None = None) -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000 if ms is None else ms
    rand = int.from_bytes(os.urandom(10), "big")

    # 12 bits of randomness in rand_a, 62 bits in rand_b.
    rand_a = (rand >> 62) & 0x0FFF
    rand_b = rand & ((1 << 62) - 1)

    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand_b
    return uuid.UUID(int=value)


def timestamp_ms(value: uuid.UUID) -> int:
    """Recover the millisecond timestamp embedded in a v7 UUID."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: version={value.version}")
    return value.int >> 80
