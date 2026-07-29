import unittest
import time
from datetime import datetime
from http.client import IncompleteRead
from unittest.mock import Mock, patch

import httpx

from infogather.sources import InfoSources


class InfoSourcesTests(unittest.TestCase):
    def test_fetch_feed_retries_incomplete_read(self) -> None:
        feed = {"entries": []}
        client = Mock()
        client.get.side_effect = [
            IncompleteRead(b"partial", 10),
            httpx.Response(200, headers={"cache-control": "max-age=60"}),
        ]
        with (
            patch("infogather.sources.feedparser.parse", return_value=feed) as parse,
            patch("infogather.sources.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            result, state = InfoSources._fetch_feed(
                client,
                "https://rss.arxiv.org/rss/math.NT",
                "Number Theory",
                attempts=2,
                delay=0,
            )

        self.assertIs(result, feed)
        self.assertGreater(state["next_fetch_at"], 0)
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(parse.call_count, 1)
        sleep.assert_called_once_with(0)

    def test_fetch_feed_reraises_incomplete_read_after_retries(self) -> None:
        errors = [IncompleteRead(b"partial", 10), IncompleteRead(b"partial", 10)]
        client = Mock()
        client.get.side_effect = errors
        with (
            patch("infogather.sources.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            with self.assertRaises(IncompleteRead):
                InfoSources._fetch_feed(
                    client,
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=2,
                    delay=0,
                )

        self.assertEqual(client.get.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_fetch_feed_retries_http_failure(self) -> None:
        feed = {"entries": []}
        client = Mock()
        client.get.side_effect = [httpx.Response(503), httpx.Response(200)]
        with (
            patch("infogather.sources.feedparser.parse", return_value=feed) as parse,
            patch("infogather.sources.time.sleep") as sleep,
            patch("builtins.print"),
        ):
            result, _ = InfoSources._fetch_feed(
                client,
                "https://rss.arxiv.org/rss/math.NT",
                "Number Theory",
                attempts=2,
                delay=0,
            )

        self.assertIs(result, feed)
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(parse.call_count, 1)
        sleep.assert_called_once_with(0)

    def test_fetch_feed_rejects_bozo_result_with_partial_entries(self) -> None:
        malformed = {
            "bozo": True,
            "bozo_exception": ValueError("truncated XML"),
            "entries": [{"id": "oai:arXiv.org:2601.00001v1"}],
        }
        client = Mock()
        client.get.return_value = httpx.Response(200)
        with (
            patch("infogather.sources.feedparser.parse", return_value=malformed),
            patch("infogather.sources.time.sleep"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid feed"):
                InfoSources._fetch_feed(
                    client,
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=2,
                    delay=0,
                )

    def test_fetch_feed_uses_conditional_etag(self) -> None:
        client = Mock()
        client.get.return_value = httpx.Response(
            304,
            headers={
                "cache-control": "max-age=120",
                "age": "90",
                "etag": "new",
            },
        )

        before = time.time()
        feed, state = InfoSources._fetch_feed(
            client,
            "https://rss.arxiv.org/rss/math.NT",
            "Number Theory",
            {"etag": "old", "last_modified": "yesterday"},
        )

        self.assertIsNone(feed)
        self.assertEqual(state["etag"], "new")
        self.assertGreaterEqual(state["next_fetch_at"], before + 29)
        self.assertLessEqual(state["next_fetch_at"], before + 31)
        client.get.assert_called_once_with(
            "https://rss.arxiv.org/rss/math.NT",
            headers={
                "If-None-Match": "old",
                "If-Modified-Since": "yesterday",
            },
        )

    def test_fetch_raw_feeds_skips_fresh_cache(self) -> None:
        url = "https://rss.arxiv.org/rss/math.NT"
        source = InfoSources(
            {"arXiv": [{"name": "Number Theory", "url": url}]},
            feed_states={url: {"next_fetch_at": time.time() + 60}},
        )

        with patch.object(source, "_fetch_feed") as fetch, patch("builtins.print"):
            feeds = source._fetch_raw_feeds()

        fetch.assert_not_called()
        self.assertEqual(feeds, {"arXiv": []})

    def test_deduplicate_entries_keeps_newest_and_merges_tags(self) -> None:
        first = {
            "srce_ty": "arXiv",
            "srce_id": "2601.00001",
            "version": 1,
            "content": {"tags": ["math.AG"]},
        }
        duplicate = {
            "srce_ty": "arXiv",
            "srce_id": "2601.00001",
            "version": 1,
            "content": {"tags": ["math.NT"]},
        }
        newer = {
            "srce_ty": "arXiv",
            "srce_id": "2601.00001",
            "version": 2,
            "content": {"tags": ["math.DG"]},
        }

        merged = InfoSources._deduplicate_entries([first, duplicate])
        latest = InfoSources._deduplicate_entries([first, newer])

        self.assertEqual(merged[0]["content"]["tags"], ["math.AG", "math.NT"])
        self.assertIs(latest[0], newer)

    def test_no_cache_response_is_immediately_stale(self) -> None:
        before = time.time()

        state = InfoSources._feed_state_from_headers(
            {"cache-control": "no-cache, max-age=86400"},
            previous={},
        )

        self.assertLessEqual(state["next_fetch_at"], before + 1)

    def test_no_store_response_discards_validators(self) -> None:
        state = InfoSources._feed_state_from_headers(
            {
                "cache-control": "no-store, max-age=86400",
                "etag": "new",
                "last-modified": "today",
            },
            previous={"etag": "old", "last_modified": "yesterday"},
        )

        self.assertIsNone(state["etag"])
        self.assertIsNone(state["last_modified"])

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

    def test_normalized_arxiv_canonicalizes_url_id(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {},
                "entries": [
                    {
                        "id": "https://arxiv.org/pdf/math/0301234v2.pdf?download=1",
                        "summary": "Standalone abstract.",
                        "tags": [{"term": "math.AG"}, {"term": None}],
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["srce_id"], "math/0301234")
        self.assertEqual(entries[0]["version"], 2)
        self.assertEqual(entries[0]["content"]["abst"], "Standalone abstract.")
        self.assertEqual(entries[0]["content"]["tags"], ["math.AG"])

    def test_normalized_arxiv_canonicalizes_prefixed_id(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {},
                "entries": [
                    {
                        "id": "arXiv:2601.00004v3",
                        "summary": "Abstract: Prefixed id.",
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)

        self.assertEqual(entries[0]["srce_id"], "2601.00004")
        self.assertEqual(entries[0]["version"], 3)

    def test_normalized_arxiv_skips_only_malformed_entry(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {},
                "entries": [
                    {"id": "missing-version", "summary": "bad"},
                    {
                        "id": "oai:arXiv.org:2601.00003v1",
                        "summary": "arXiv:2601.00003v1\r\nAbstract: Valid.",
                    },
                ],
            }
        ]

        with patch("builtins.print"):
            entries = source._normalized_arXiv(feeds)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["srce_id"], "2601.00003")
        self.assertEqual(entries[0]["content"]["abst"], "Valid.")
