"""Schema-level guarantees that the rest of the system is allowed to assume."""

from __future__ import annotations

import datetime as dt

import asyncpg
import pytest
from mmp_core.ids import uuid7
from mmp_db.base import Base, OrgScopedMixin
from mmp_db.models import *  # noqa: F403 — registers the metadata


def test_every_table_has_a_primary_key():
    missing = [name for name, table in Base.metadata.tables.items() if not table.primary_key]
    assert not missing, f"tables without a primary key: {missing}"


def test_org_scoped_tables_carry_the_column_they_are_filtered_on():
    """The mixin and the column must agree, or the RLS policy is nonsense."""
    offenders = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, OrgScopedMixin) and "organization_id" not in mapper.columns:
            offenders.append(cls.__tablename__)
    assert not offenders, f"OrgScopedMixin without organization_id: {offenders}"


def test_revenue_is_never_a_float():
    """Money is integer minor units. A float revenue total is an invoice dispute."""
    for name, table in Base.metadata.tables.items():
        for column in table.columns:
            if "revenue" in column.name or "amount" in column.name:
                assert "FLOAT" not in str(column.type).upper(), (
                    f"{name}.{column.name} stores money as {column.type}"
                )


async def test_one_install_yields_one_attribution(owner_conn, two_orgs):
    """The invariant the product's numbers rest on, enforced by the database.

    Simulates the real race: two workers, both handed the same install, both
    convinced they should write the attribution.
    """
    org_a, _ = two_orgs
    app_id = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    now = dt.datetime.now(dt.UTC)
    install_key = f"{app_id}:anon-race"

    insert = """
        INSERT INTO attributions (id, organization_id, app_id, install_key, anonymous_id,
                                  method, installed_at, attributed_at, window_days,
                                  expires_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'anon-race', $5, $6, $6, 7, $7, $6, $6)
    """
    expires = now + dt.timedelta(days=7)
    await owner_conn.execute(insert, uuid7(), org_a, app_id, install_key, "referrer", now, expires)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await owner_conn.execute(
            insert, uuid7(), org_a, app_id, install_key, "click_id", now, expires
        )

    count = await owner_conn.fetchval(
        "SELECT count(*) FROM attributions WHERE install_key = $1 AND superseded_by IS NULL",
        install_key,
    )
    assert count == 1


async def test_reattribution_supersedes_rather_than_overwrites(owner_conn, two_orgs):
    """History is append-only: a corrected attribution must not erase the old one."""
    org_a, _ = two_orgs
    app_id = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    now = dt.datetime.now(dt.UTC)
    expires = now + dt.timedelta(days=7)
    install_key = f"{app_id}:anon-supersede"
    original_id, replacement_id = uuid7(), uuid7()

    insert = """
        INSERT INTO attributions (id, organization_id, app_id, install_key, anonymous_id,
                                  method, installed_at, attributed_at, window_days,
                                  expires_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'anon-supersede', $5, $6, $6, 7, $7, $6, $6)
    """
    await owner_conn.execute(
        insert, original_id, org_a, app_id, install_key, "organic", now, expires
    )

    # The swap has to happen in one transaction, in this order: the partial
    # unique index forbids two current rows, and the deferred foreign key is
    # what allows pointing at a row that does not exist yet.
    async with owner_conn.transaction():
        await owner_conn.execute(
            "UPDATE attributions SET superseded_by = $1 WHERE id = $2",
            replacement_id,
            original_id,
        )
        await owner_conn.execute(
            insert, replacement_id, org_a, app_id, install_key, "referrer", now, expires
        )

    rows = await owner_conn.fetch(
        "SELECT method, superseded_by FROM attributions WHERE install_key = $1", install_key
    )
    assert len(rows) == 2, "the original attribution must still exist"
    current = [r for r in rows if r["superseded_by"] is None]
    assert len(current) == 1 and current[0]["method"] == "referrer"


async def test_attribution_window_is_bounded(owner_conn, two_orgs):
    """An unbounded window turns every install into a full-history click scan."""
    org_a, _ = two_orgs
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await owner_conn.execute(
            """INSERT INTO apps (id, organization_id, name, platform, status,
                                 install_window_days, event_window_days,
                                 session_timeout_minutes, timezone, created_at, updated_at)
               VALUES ($1, $2, 'bad', 'android', 'active', 3650, 30, 30, 'UTC', now(), now())""",
            uuid7(),
            org_a,
        )


async def test_postback_delivery_is_idempotent_per_rule_and_event(owner_conn, two_orgs):
    """A redelivered queue message must not fire a second conversion."""
    org_a, _ = two_orgs
    app_id = await owner_conn.fetchval(
        "SELECT id FROM apps WHERE organization_id = $1 LIMIT 1", org_a
    )
    rule_id, event_id = uuid7(), uuid7()
    await owner_conn.execute(
        """INSERT INTO postback_rules (id, organization_id, app_id, name, trigger_event,
                                       method, url_template, success_status_codes,
                                       requires_attribution, is_sandbox, enabled,
                                       created_at, updated_at)
           VALUES ($1, $2, $3, 'r', 'purchase', 'GET', 'https://example.com/p',
                   '[200]'::jsonb, true, false, true, now(), now())""",
        rule_id,
        org_a,
        app_id,
    )
    insert = """INSERT INTO postback_deliveries (id, organization_id, postback_rule_id,
                                                 event_id, status, attempt_count, created_at)
                VALUES ($1, $2, $3, $4, 'pending', 0, now())"""
    await owner_conn.execute(insert, uuid7(), org_a, rule_id, event_id)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await owner_conn.execute(insert, uuid7(), org_a, rule_id, event_id)
