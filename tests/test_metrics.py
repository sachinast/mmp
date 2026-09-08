"""Metrics.

The rule that decides what belongs: a metric exists to answer a question someone
will ask at 3am. Everything else is cardinality and cost.
"""

from __future__ import annotations

import re

import pytest

from mmp_core import metrics


def test_no_per_tenant_labels():
    """The first thing anyone reaches for, and how a metrics bill becomes a
    surprise.

    A counter labelled by app_id has as many series as there are apps; a
    histogram labelled by app_id has that times its buckets. Per-tenant numbers
    belong in the rollups, which are built for it and queried on demand rather
    than scraped every fifteen seconds.
    """
    forbidden = {
        "app_id",
        "organization_id",
        "user_id",
        "anonymous_id",
        "campaign_id",
        "tracking_code",
        "event_name",
    }
    offenders = []
    for collector in list(metrics.REGISTRY._collector_to_names):
        labels = set(getattr(collector, "_labelnames", ()) or ())
        if labels & forbidden:
            offenders.append((getattr(collector, "_name", collector), labels & forbidden))
    assert not offenders, f"unbounded labels: {offenders}"


def test_rejection_reasons_are_a_closed_vocabulary():
    """A label taken from an exception message is an unbounded label, and an
    unbounded label is a metrics outage waiting for the right bad request."""
    metrics.observe_rejection("rate_limited")
    metrics.observe_rejection("a reason nobody defined")
    metrics.observe_rejection("another one")

    rendered = metrics.render().decode()
    reasons = set(re.findall(r'mmp_events_rejected_total\{reason="([^"]+)"\}', rendered))
    assert "rate_limited" in reasons
    assert "other" in reasons
    assert reasons <= set(metrics.REJECTION_REASONS) | {"other"}


def test_redirect_buckets_cover_the_interesting_range():
    """The library defaults put most of their resolution above a second, where
    nothing of ours should ever be. The SLO is 120 ms."""
    buckets = metrics.redirect_latency._upper_bounds
    under_slo = [b for b in buckets if b <= 0.12]
    assert len(under_slo) >= 6, "most resolution should sit below the SLO"


def test_render_is_valid_exposition_format():
    metrics.events_accepted.labels(source="sdk").inc()
    rendered = metrics.render().decode()
    assert "# HELP mmp_events_accepted_total" in rendered
    assert "# TYPE mmp_events_accepted_total counter" in rendered


def test_a_metrics_failure_does_not_break_the_service(caplog):
    """A metrics failure that takes down the thing being measured is a famous
    own goal."""
    import asyncio

    class Broken:
        async def xlen(self, *_args):
            raise ConnectionError("redis is unavailable")

        async def xpending(self, *_args):
            raise ConnectionError("redis is unavailable")

    # Must not raise.
    asyncio.get_event_loop_policy()
    asyncio.run(metrics.sample_stream_depths(Broken(), {"stream:events": "g"}))


async def test_the_tracker_exposes_metrics(tracker, seeded_app):
    from tests.conftest_ingest import ANDROID_UA

    await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    response = await tracker.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "mmp_redirects_total" in response.text
    assert 'outcome="redirected"' in response.text


async def test_redirect_latency_is_observed(tracker, seeded_app):
    """The SLO number. If it is not measured it is not a commitment."""
    from tests.conftest_ingest import ANDROID_UA

    before = metrics.redirect_latency._sum.get()
    await tracker.get(
        f"/c/{seeded_app['tracking_code']}",
        headers={"user-agent": ANDROID_UA},
        follow_redirects=False,
    )
    assert metrics.redirect_latency._sum.get() > before


@pytest.mark.parametrize(
    "name",
    [
        "mmp_events_accepted_total",
        "mmp_events_written_total",
        "mmp_stream_backlog",
        "mmp_redirects_total",
        "mmp_attributions_total",
        "mmp_deliveries_total",
        "mmp_pipeline_drift",
    ],
)
def test_the_operational_metrics_exist(name):
    """Each of these answers a specific 3am question: has ingestion stopped, are
    we behind, did the match rate drop, are postbacks failing, are we losing
    events silently."""
    assert name in metrics.render().decode()
