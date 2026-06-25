import unittest
from datetime import datetime
from http.client import IncompleteRead
from unittest.mock import patch

from infogather.sources import InfoSources


class InfoSourcesTests(unittest.TestCase):
    def test_fetch_feed_retries_incomplete_read(self) -> None:
        feed = {"entries": []}
        with (
            patch(
                "infogather.sources.feedparser.parse",
                side_effect=[IncompleteRead(b"partial", 10), feed],
            ) as parse,
            patch("infogather.sources.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            result = InfoSources._fetch_feed(
                "https://rss.arxiv.org/rss/math.NT",
                "Number Theory",
                attempts=2,
                delay=0,
            )

        self.assertIs(result, feed)
        self.assertEqual(parse.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_fetch_feed_reraises_incomplete_read_after_retries(self) -> None:
        errors = [IncompleteRead(b"partial", 10), IncompleteRead(b"partial", 10)]
        with (
            patch(
                "infogather.sources.feedparser.parse",
                side_effect=errors,
            ) as parse,
            patch("infogather.sources.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            with self.assertRaises(IncompleteRead):
                InfoSources._fetch_feed(
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=2,
                    delay=0,
                )

        self.assertEqual(parse.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_normalized_arxiv_falls_back_when_feed_timestamp_missing(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {},
                "entries": [
                    {
                        "id": "oai:arXiv.org:2601.00001v1",
                        "summary": "arXiv:2601.00001v1\nAbstract: Example abstract.",
                        "tags": [{"term": "math.AG"}],
                        "link": "https://arxiv.org/abs/2601.00001",
                        "title": "Example title",
                        "author": "Example author",
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["srce_id"], "2601.00001")
        # Must be parseable ISO datetime even when feed timestamp is absent.
        datetime.fromisoformat(entries[0]["updated"])

    def test_normalized_arxiv_ignores_invalid_feed_timestamp(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {"updated_parsed": [2026, 3, 1]},
                "entries": [
                    {
                        "id": "oai:arXiv.org:2601.00002v2",
                        "summary": "arXiv:2601.00002v2\nAbstract: Another abstract.",
                        "tags": [{"term": "math.AG"}],
                        "link": "https://arxiv.org/abs/2601.00002",
                        "title": "Another title",
                        "author": "Another author",
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], 2)
        datetime.fromisoformat(entries[0]["updated"])
