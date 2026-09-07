"""Garbage-collection tuning for the latency-sensitive services.

Python's generational collector is the tracker's largest single source of tail
latency. Measured on the ingest benchmark before any tuning:

    p50 2.6 ms,  p95 4.0 ms,  p99 17.2 ms

The p99 was not the handler, the flush, or Redis — varying all three moved it
not at all. It was two generation-2 collections, each pausing the event loop for
about 11 ms. A request unlucky enough to arrive during one waits out the whole
pause, which is why the tail was both large and remarkably consistent.

Two mitigations, both cheap:

**Freeze the startup heap.** Everything allocated while importing modules and
building the application — thousands of code objects, types and constants — is
permanent, and a gen-2 collection traverses all of it every time to prove that.
``gc.freeze()`` moves it to a permanent generation that collections skip. Called
once, after startup, before serving.

**Collect less often.** The default gen-0 threshold of 700 allocations is tuned
for scripts. A service that allocates a few hundred short-lived objects per
request crosses it constantly, and each gen-0 collection is a step towards the
gen-2 collection that actually hurts.

This does *not* disable garbage collection. Reference counting still frees the
overwhelming majority of objects immediately; the cyclic collector still runs,
just less often and over less. Long-running processes that genuinely create
reference cycles still get them collected.
"""

from __future__ import annotations

import gc

from mmp_core.logging import get_logger

log = get_logger(__name__)

# Roughly 20x the default gen-0 threshold. Large enough that a request's
# allocations rarely trigger a collection on their own, small enough that a
# leaking cycle is still found in seconds rather than hours.
GEN0_THRESHOLD = 15_000
GEN1_THRESHOLD = 25
GEN2_THRESHOLD = 25


def tune_for_latency() -> dict[str, object]:
    """Freeze the startup heap and reduce collection frequency.

    Call once, after the application is constructed and before it serves
    traffic. Returns what it did, so a service can log it — tuning that happens
    invisibly is tuning nobody can rule out when investigating a latency
    regression later.
    """
    # Collect first: this promotes anything still live from startup into the
    # oldest generation so that freeze() actually captures it.
    collected = gc.collect()
    gc.freeze()
    previous = gc.get_threshold()
    gc.set_threshold(GEN0_THRESHOLD, GEN1_THRESHOLD, GEN2_THRESHOLD)

    result = {
        "collected_at_startup": collected,
        "frozen_objects": gc.get_freeze_count(),
        "previous_threshold": previous,
        "threshold": gc.get_threshold(),
    }
    log.info("gc_tuned_for_latency", **result)
    return result
