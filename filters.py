from datetime import datetime, timedelta, timezone


class InfoFilters:
    @staticmethod
    def filter_true(entry: dict) -> bool:
        """default filter: allow all entries"""

        return True

    @staticmethod
    def filter_one_day(entry: dict) -> bool:
        """one day filter: entries updated in the last day"""

        try:
            updated = datetime.fromisoformat(entry.get("updated", ""))
            return datetime.now(timezone.utc) - updated <= timedelta(days=1)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def filter_favored(entry: dict) -> bool:
        """favored filter: favored entries, regardless of recency"""

        return bool(entry["favored"])

    @staticmethod
    def filter_ingestion(entry: dict) -> bool:
        """ingestion filter: must be within one day, new or favored updated"""

        new_or_fav = entry["version"] == 1 or entry["favored"] == 1
        return InfoFilters.filter_one_day(entry) and new_or_fav
