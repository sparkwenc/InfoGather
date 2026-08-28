import unittest
from datetime import timezone

from infogather.filters import parse_updated


class InfoFilterTests(unittest.TestCase):
    def test_parse_updated_treats_naive_timestamp_as_utc(self) -> None:
        parsed = parse_updated("2026-03-01T12:00:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_parse_updated_rejects_utc_normalization_overflow(self) -> None:
        self.assertIsNone(parse_updated("0001-01-01T00:00:00+14:00"))
