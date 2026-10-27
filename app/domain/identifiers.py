"""Domain-owned generators for newly created persistent identifiers."""

from uuid import UUID

from uuid_utils import uuid7 as _uuid7


def new_uuid7() -> UUID:
    """Return a standard-library UUID instance using UUID version 7."""
    return UUID(str(_uuid7()))
