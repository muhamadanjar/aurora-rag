from typing import Any

from fastapi import HTTPException

from app.domain.dataset.entities import Dataset, Example, Revision


def entity_etag(value: Any) -> str:
    if isinstance(value, (Dataset, Example, Revision)):
        return f'"{value.version}"'
    timestamp = getattr(value, "updated_at", None) or getattr(value, "created_at")
    return f'"{timestamp.timestamp():.6f}"'


def parse_if_match(header: str | None) -> int | None:
    """Translate an If-Match header into an expected resource version."""
    if header is None:
        raise HTTPException(status_code=428, detail="If-Match header is required")
    if header == "*":
        return None
    try:
        return int(header.strip('"'))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=412, detail="Resource has changed") from exc
