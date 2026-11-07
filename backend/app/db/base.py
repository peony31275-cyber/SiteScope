"""Declarative base shared by all ORM models."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so migrations (and their downgrades) can address every
# constraint by name. Partial and special-purpose indexes are named explicitly instead.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Every timestamp is a timezone-aware TIMESTAMPTZ; asyncpg returns UTC-aware datetimes.
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy declarative API
        datetime: DateTime(timezone=True),
        uuid.UUID: Uuid(),
        dict[str, Any]: JSONB(),
    }
