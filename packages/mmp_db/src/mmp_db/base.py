"""Declarative base and the mixins every table is built from."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from mmp_core.ids import uuid7
from sqlalchemy import DateTime, ForeignKey, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit, predictable constraint names. Without this, Alembic autogenerate
# produces migrations that drop and recreate constraints it cannot name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # SQLAlchemy reads this off the class; it is a declarative directive,
    # not mutable per-instance state.
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


def uuid_pk() -> Mapped[uuid.UUID]:
    """A UUIDv7 primary key, minted in Python.

    Generated application-side rather than by the database so that the value is
    known before the INSERT — the ingest path needs the ID to correlate a queued
    job with the row it will become, and a round-trip to fetch it would be a
    round-trip per event.
    """
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgScopedMixin:
    """Marks a table as tenant-owned.

    This flag is what the RLS tooling keys off: every model carrying the mixin
    must have a row-level security policy, and a test walks the metadata to
    prove none was forgotten. Adding a tenant table without isolation is then a
    build failure rather than a silent data-exposure bug.
    """

    __org_scoped__ = True


def org_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def short_text(length: int = 255, **kwargs: Any) -> Mapped[str]:
    return mapped_column(String(length), **kwargs)


# Enumerated values are stored as text with a CHECK constraint rather than as a
# native Postgres ENUM. Adding a value to a native enum needs ALTER TYPE, which
# makes every new status a schema migration with locking implications; a CHECK
# constraint is a one-line change and reads the same from the application.
def one_of(column: str, *allowed: str) -> str:
    values = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({values})"
