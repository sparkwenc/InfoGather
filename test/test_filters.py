import unittest
from datetime import datetime, timedelta, timezone

from infogather.filters import parse_updated, updated_within


class InfoFilterTests(unittest.TestCase):
    def test_parse_updated_treats_naive_timestamp_as_utc(self) -> None:
        parsed = parse_updated("2026-03-01T12:00:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_updated_within_rejects_future_timestamp(self) -> None:
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)

        self.assertFalse(
            updated_within(
                "2026-03-02T00:00:00+00:00",
                timedelta(days=7),
                now=now,
            )
        )

    def test_parse_updated_rejects_utc_normalization_overflow(self) -> None:
        self.assertIsNone(parse_updated("0001-01-01T00:00:00+14:00"))


if __name__ == "__main__":
    unittest.main()
