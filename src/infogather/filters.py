from datetime import datetime, timezone


def parse_updated(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        updated = datetime.fromisoformat(value)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return updated.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
