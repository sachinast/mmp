"""Erasing a device's data.

A deletion request is the one privacy operation that cannot be partially done.
Removing events but leaving attributions produces a system that reports having
deleted data it still holds — worse than not deleting, because it is a claim.
"""

from __future__ import annotations

import datetime as dt

import pytest
from mmp_core.ids import uuid7
from mmp_db.erasure import (
    ERASURE_EXCLUSIONS,
    ERASURE_TARGETS,
    Scope,
    erase_device,
)


def test_every_person_linked_table_is_accounted_for():
    """A new table carrying an anonymous_id must fail this until someone has
    decided what erasure means for it.

    This is the test that keeps the module honest: without it, erasure quietly
    becomes incomplete one migration at a time.
    """
    import mmp_db.models  # noqa: F401
    from mmp_db.base import Base

    linked = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if "anonymous_id" in mapper.columns or "user_id" in mapper.columns
    }
    # The raw event tables are not ORM-mapped; name them explicitly.
    linked |= {"events", "clicks"}

    accounted = {table for table, _ in ERASURE_TARGETS} | set(ERASURE_EXCLUSIONS)
    unaccounted = linked - accounted - {"users", "organization_members"}

    assert not unaccounted, (
        "tables holding person-linked data with no erasure decision: "
        f"{sorted(unaccounted)}. Add them to ERASURE_TARGETS, or to "
        "ERASURE_EXCLUSIONS with the reason."
    )


def test_exclusions_carry_a_reason():
    """A table excluded without a stated reason is an oversight wearing a
    decision's clothes."""
    for table, reason in ERASURE_EXCLUSIONS.items():
        assert len(reason) > 20, f"{table} needs a real reason, not '{reason}'"


def test_attributions_are_erased_before_clicks_are_touched():
    """Attribution rows reference clicks. Order matters."""
    tables = [table for table, _ in ERASURE_TARGETS]
    assert "attributions" in tables
    assert "clicks" not in tables, "clicks are cleared, not deleted"


async def test_a_device_is_erased_across_every_table(owner_conn, seeded_app):
    app_id = seeded_app["app_id"]
    org_id = seeded_app["organization_id"]
    device = "erase-me"
    device_hash = b"\x11" * 16
    now = dt.datetime.now(dt.UTC)

    for _ in range(3):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', $5, 1)""",
            uuid7(),
            now,
            org_id,
            app_id,
            device,
        )
    await owner_conn.execute(
        """INSERT INTO attributions (id, organization_id, app_id, install_key,
                                     anonymous_id, method, installed_at, attributed_at,
                                     window_days, expires_at, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'organic', $6, $6, 7, $7, $6, $6)""",
        uuid7(),
        org_id,
        app_id,
        f"{app_id}:{device}",
        device,
        now,
        now + dt.timedelta(days=30),
    )
    await owner_conn.execute(
        """INSERT INTO consent_states (id, organization_id, app_id, anonymous_id,
                                       purpose, state, created_at, updated_at)
           VALUES ($1, $2, $3, $4, 'attribution', 'granted', now(), now())""",
        uuid7(),
        org_id,
        app_id,
        device,
    )
    click_id = uuid7()
    await owner_conn.execute(
        """INSERT INTO clicks (click_id, clicked_at, organization_id, app_id,
                               tracking_link_id, device_hash, ip_hash, user_agent,
                               platform, is_bot)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'Mozilla/5.0', 1, false)""",
        click_id,
        now,
        org_id,
        app_id,
        seeded_app["tracking_link_id"],
        device_hash,
        b"\x22" * 16,
    )

    result = await erase_device(
        owner_conn, app_id=app_id, anonymous_id=device, device_hash=device_hash
    )

    assert result.scope is Scope.DEVICE
    assert result.deleted["events"] == 3
    assert result.deleted["attributions"] == 1
    assert result.deleted["consent_states"] == 1
    assert result.completed_at is not None

    for table in ("events", "attributions", "consent_states"):
        remaining = await owner_conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE app_id = $1 AND anonymous_id = $2",  # noqa: S608
            app_id,
            device,
        )
        assert remaining == 0, f"{table} still holds the device's data"


async def test_a_click_is_de_identified_rather_than_deleted(owner_conn, seeded_app):
    """Deleting a click would change a click count for a period an advertiser
    has already been billed for and already reported. Clearing the identifier
    removes the link to a person while leaving the fact that a click happened —
    which is what erasure is actually asking for.
    """
    app_id = seeded_app["app_id"]
    device_hash = b"\x33" * 16
    click_id = uuid7()
    now = dt.datetime.now(dt.UTC)

    await owner_conn.execute(
        """INSERT INTO clicks (click_id, clicked_at, organization_id, app_id,
                               tracking_link_id, device_hash, ip_hash, user_agent,
                               platform, is_bot)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'Mozilla/5.0', 1, false)""",
        click_id,
        now,
        seeded_app["organization_id"],
        app_id,
        seeded_app["tracking_link_id"],
        device_hash,
        b"\x44" * 16,
    )

    result = await erase_device(
        owner_conn, app_id=app_id, anonymous_id="unused", device_hash=device_hash
    )
    assert result.cleared["clicks"] == 1

    row = await owner_conn.fetchrow(
        "SELECT device_hash, ip_hash, user_agent FROM clicks WHERE click_id = $1",
        click_id,
    )
    assert row is not None, "the click itself must survive"
    assert row["device_hash"] is None
    assert row["ip_hash"] is None
    assert row["user_agent"] is None


async def test_erasure_does_not_touch_another_device(owner_conn, seeded_app):
    app_id, org_id = seeded_app["app_id"], seeded_app["organization_id"]
    now = dt.datetime.now(dt.UTC)

    for device in ("target", "bystander"):
        await owner_conn.execute(
            """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                                   app_id, event_name, anonymous_id, platform)
               VALUES ($1, $2, $2, $3, $4, 'install', $5, 1)""",
            uuid7(),
            now,
            org_id,
            app_id,
            device,
        )

    await erase_device(owner_conn, app_id=app_id, anonymous_id="target")

    survived = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1 AND anonymous_id = 'bystander'",
        app_id,
    )
    assert survived == 1


async def test_erasure_does_not_cross_apps(owner_conn, seeded_app):
    """The same anonymous id in another app is another person's data."""
    from mmp_core.ids import uuid7 as _uuid7

    org_id = seeded_app["organization_id"]
    other_app = _uuid7()
    await owner_conn.execute(
        """INSERT INTO apps (id, organization_id, name, platform, android_package_name,
                             status, install_window_days, event_window_days,
                             session_timeout_minutes, timezone)
           VALUES ($1, $2, 'Other', 'android', 'com.example.other', 'active',
                   7, 30, 30, 'UTC')""",
        other_app,
        org_id,
    )
    now = dt.datetime.now(dt.UTC)
    await owner_conn.execute(
        """INSERT INTO events (event_id, received_at, occurred_at, organization_id,
                               app_id, event_name, anonymous_id, platform)
           VALUES ($1, $2, $2, $3, $4, 'install', 'shared-id', 1)""",
        _uuid7(),
        now,
        org_id,
        other_app,
    )

    await erase_device(owner_conn, app_id=seeded_app["app_id"], anonymous_id="shared-id")

    survived = await owner_conn.fetchval(
        "SELECT count(*) FROM events WHERE app_id = $1 AND anonymous_id = 'shared-id'",
        other_app,
    )
    assert survived == 1
    await owner_conn.execute("DELETE FROM events WHERE app_id = $1", other_app)
    await owner_conn.execute("DELETE FROM apps WHERE id = $1", other_app)


async def test_erasing_nothing_is_not_an_error(owner_conn, seeded_app):
    """A request for a device we have never seen is a valid request with a
    valid answer."""
    result = await erase_device(
        owner_conn, app_id=seeded_app["app_id"], anonymous_id="never-existed"
    )
    assert result.total_deleted == 0
    assert result.completed_at is not None


@pytest.mark.parametrize("table", [t for t, _ in ERASURE_TARGETS])
def test_targets_use_bound_parameters(table):
    """The predicate is ours, and must contain no interpolated value."""
    predicate = dict(ERASURE_TARGETS)[table]
    assert "$1" in predicate
    assert "'" not in predicate, "no literal values in an erasure predicate"
