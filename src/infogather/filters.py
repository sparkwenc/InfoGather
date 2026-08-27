from datetime import datetime, timedelta, timezone


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


def updated_within(
    value: object,
    window: timedelta,
    *,
    now: datetime | None = None,
) -> bool:
    updated = parse_updated(value)
    if updated is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - updated
    return timedelta(0) <= age <= window


def filter_true(_: dict) -> bool:
    """default filter: allow all entries"""

    return True


def filter_one_day(entry: dict) -> bool:
    """one day filter: entries updated in the last day"""

    return updated_within(entry.get("updated"), timedelta(days=1))


def filter_favored(entry: dict) -> bool:
    """favored filter: favored entries, regardless of recency"""

    return bool(entry["favored"])


def filter_ingestion(entry: dict) -> bool:
    """ingestion filter: must be within one day, new or favored updated"""

    new_or_fav = entry["version"] == 1 or entry["favored"] == 1
    return filter_one_day(entry) and new_or_fav
