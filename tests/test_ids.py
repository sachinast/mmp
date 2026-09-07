"""UUIDv7 has to be genuinely time-ordered — the index strategy depends on it."""

import time
import uuid

from mmp_core.ids import timestamp_ms, uuid7


def test_version_and_variant():
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10  # RFC 9562 variant


def test_monotonic_across_milliseconds():
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    assert first < second, "v7 values must sort by mint time"


def test_timestamp_roundtrip():
    now_ms = int(time.time() * 1000)
    value = uuid7(ms=now_ms)
    assert timestamp_ms(value) == now_ms


def test_timestamp_rejects_v4():
    try:
        timestamp_ms(uuid.uuid4())
    except ValueError:
        return
    raise AssertionError("timestamp_ms must reject a non-v7 UUID")


def test_uniqueness():
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000
