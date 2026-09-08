"""The traffic-level fraud sweep, against real data in a real database.

The rules themselves are unit-tested in ``test_fraud_rules``. What is tested
here is the part that can only go wrong against SQL: whether the statistics
handed to those rules are the statistics they were meant to get. An early
version of this query joined the per-device and per-IP aggregates directly onto
the install rows, which multiplied every install by the number of devices on its
link — inflating the totals enough to manufacture a flooding finding out of
nothing but arithmetic. Every count below is asserted exactly, which is what
catches that class of bug.
"""

from __future__ import annotations

import datetime as dt

from mmp_core.ids import uuid7

# Every timestamp stays inside the live daily partitions, so "late" is driven by
# an explicit threshold rather than by back-dating clicks out of the table.
LATE = dt.timedelta(minutes=30)


async def _findings(conn, seeded_app) -> int:
    """Scoped to this app, deliberately. The suite shares one database and does
    not truncate between tests, so a global count here would make these tests
    pass or fail depending on what ran before them."""
    return await conn.fetchval(
        "SELECT count(*) FROM fraud_findings WHERE app_id = $1", seeded_app["app_id"]
    )


async def _worker_db(seeded_app):
    from mmp_db.pool import Database

    return await Database.connect(seeded_app["worker_settings"], role="mmp_worker")


async def _seed(conn, seeded_app, *, installs, gap, device_hashes=None, ip_hashes=None):
    """Write `installs` click/attribution pairs `gap` apart on the seeded link."""
    now = dt.datetime.now(dt.UTC)
    for i in range(installs):
        click_id, attribution_id = uuid7(), uuid7()
        clicked_at = now - dt.timedelta(minutes=90)
        installed_at = clicked_at + gap
        device = (device_hashes or [bytes([i % 256]) * 32])[i % len(device_hashes or [1])]
        ip = (ip_hashes or [bytes([i % 256]) * 32])[i % len(ip_hashes or [1])]
        await conn.execute(
            """INSERT INTO clicks (click_id, clicked_at, organization_id, app_id, campaign_id,
                                   tracking_link_id, device_hash, ip_hash, platform, is_bot)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, false)""",
            click_id,
            clicked_at,
            seeded_app["organization_id"],
            seeded_app["app_id"],
            seeded_app["campaign_id"],
            seeded_app["tracking_link_id"],
            device,
            ip,
        )
        await conn.execute(
            """INSERT INTO attributions (id, organization_id, app_id, install_key, anonymous_id,
                                         click_id, campaign_id, tracking_link_id, method,
                                         installed_at, attributed_at, window_days, expires_at,
                                         created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'referrer', $9, now(), 7,
                       $9::timestamptz + interval '30 days', now(), now())""",
            attribution_id,
            seeded_app["organization_id"],
            seeded_app["app_id"],
            f"{seeded_app['app_id']}:sweep-{i}",
            f"sweep-{i}",
            click_id,
            seeded_app["campaign_id"],
            seeded_app["tracking_link_id"],
            installed_at,
        )
    return now


async def test_prompt_traffic_produces_no_findings(owner_conn, seeded_app):
    from mmp_worker.fraud_sweep import sweep

    now = await _seed(owner_conn, seeded_app, installs=60, gap=dt.timedelta(minutes=20))
    database = await _worker_db(seeded_app)
    try:
        result = await sweep(database, now=now, late_threshold=LATE)
    finally:
        await database.close()

    assert result.links_examined == 1
    assert result.findings_written == 0, "healthy traffic must not be accused of anything"


async def test_the_statistics_are_not_inflated_by_the_joins(owner_conn, seeded_app):
    """The regression test for the row-fanout bug.

    Sixty installs spread over five devices behind two IPs. If the aggregates
    are joined before being collapsed, `attributed_installs` comes back as a
    multiple of sixty and the flooding rule fires on traffic that is fine.
    """
    from mmp_worker.fraud_sweep import STATS_SQL

    devices = [bytes([d]) * 32 for d in range(5)]
    ips = [bytes([200 + n]) * 32 for n in range(2)]
    now = await _seed(
        owner_conn,
        seeded_app,
        installs=60,
        gap=dt.timedelta(minutes=20),
        device_hashes=devices,
        ip_hashes=ips,
    )

    row = await owner_conn.fetchrow(STATS_SQL, now - dt.timedelta(days=7), now, LATE)
    assert row["attributed_installs"] == 60, "the join must not multiply the install count"
    assert row["late_installs"] == 0
    assert row["max_installs_per_device"] == 12, "60 installs across 5 devices"
    assert row["max_devices_per_ip"] == 5, "5 devices, each appearing behind both IPs"


async def test_a_flooding_link_is_found_and_explained(owner_conn, seeded_app):
    from mmp_worker.fraud_sweep import sweep

    now = await _seed(owner_conn, seeded_app, installs=60, gap=dt.timedelta(minutes=60))
    database = await _worker_db(seeded_app)
    try:
        result = await sweep(database, now=now, late_threshold=LATE)
    finally:
        await database.close()

    assert "click_flooding" in result.by_rule
    finding = await owner_conn.fetchrow(
        "SELECT rule, detail, evidence FROM fraud_findings "
        "WHERE rule = 'click_flooding' AND app_id = $1",
        seeded_app["app_id"],
    )
    assert finding is not None, "the finding must be persisted, not only counted"
    assert "60 installs" in finding["detail"] or "100%" in finding["detail"]


async def test_re_running_the_sweep_corrects_rather_than_duplicates(owner_conn, seeded_app):
    """A sweep gets re-run after every crash and deploy, and its output is shown
    to customers. Twice through must leave one finding, not two."""
    from mmp_worker.fraud_sweep import sweep

    now = await _seed(owner_conn, seeded_app, installs=60, gap=dt.timedelta(minutes=60))
    database = await _worker_db(seeded_app)
    try:
        await sweep(database, now=now, late_threshold=LATE)
        await sweep(database, now=now, late_threshold=LATE)
    finally:
        await database.close()

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM fraud_findings WHERE rule = 'click_flooding' AND app_id = $1",
        seeded_app["app_id"],
    )
    assert count == 1, f"the sweep duplicated its own finding ({count} rows)"


async def test_a_link_that_stops_misbehaving_stops_being_accused(owner_conn, seeded_app):
    """A finding left standing after the behaviour stopped is worse than none —
    the first thing a customer learns is that the reporting is stale."""
    from mmp_worker.fraud_sweep import sweep

    now = await _seed(owner_conn, seeded_app, installs=60, gap=dt.timedelta(minutes=60))
    database = await _worker_db(seeded_app)
    try:
        await sweep(database, now=now, late_threshold=LATE)
        assert await _findings(owner_conn, seeded_app) == 1

        # The late installs are re-dated so the same link now converts promptly.
        # The same link now converts promptly rather than an hour later.
        await owner_conn.execute(
            "UPDATE attributions SET installed_at = installed_at - interval '55 minutes' "
            "WHERE app_id = $1",
            seeded_app["app_id"],
        )
        await sweep(database, now=now, late_threshold=LATE)
    finally:
        await database.close()

    assert await _findings(owner_conn, seeded_app) == 0
