import io
import json
import unittest
import time
import feedparser
from datetime import datetime
from http.client import IncompleteRead
from urllib.error import HTTPError
from unittest.mock import patch

from infogather.sources import InfoSources, MAX_FEED_BYTES


class Response(io.BytesIO):
    def __init__(self, body: bytes = b"", *, status: int = 200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class InfoSourcesTests(unittest.TestCase):
    def test_fetch_feed_retries_transient_failures(self) -> None:
        failures = [
            IncompleteRead(b"partial", 10),
            HTTPError("url", 503, "unavailable", {}, None),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                patch(
                    "infogather.sources.urlopen",
                    side_effect=[failure, Response(headers={"cache-control": "max-age=60"})],
                ) as open_url,
                patch("infogather.sources.feedparser.parse", return_value={"entries": []}),
                patch("infogather.sources.time.sleep") as sleep,
                patch("builtins.print"),
            ):
                result, state = InfoSources._fetch_feed(
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=2,
                    delay=0,
                )

            self.assertEqual(result, {"entries": []})
            self.assertGreater(state["next_fetch_at"], 0)
            self.assertEqual(open_url.call_count, 2)
            sleep.assert_called_once_with(0)

    def test_fetch_feed_reraises_incomplete_read_after_retries(self) -> None:
        errors = [IncompleteRead(b"partial", 10), IncompleteRead(b"partial", 10)]
        with (
            patch("infogather.sources.urlopen", side_effect=errors) as open_url,
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

        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_fetch_feed_rejects_oversized_response(self) -> None:
        response = Response(headers={"content-length": str(MAX_FEED_BYTES + 1)})
        with patch("infogather.sources.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "too large"):
                InfoSources._fetch_feed(
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                )

    def test_fetch_feed_streaming_cap_handles_chunked_response(self) -> None:
        response = Response(b"x" * (MAX_FEED_BYTES + 1))
        with patch("infogather.sources.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "too large"):
                InfoSources._fetch_feed(
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=1,
                )

    def test_fetch_feed_rejects_bozo_result_with_partial_entries(self) -> None:
        malformed = {
            "bozo": True,
            "bozo_exception": ValueError("truncated XML"),
            "entries": [{"id": "oai:arXiv.org:2601.00001v1"}],
        }
        with (
            patch(
                "infogather.sources.urlopen",
                side_effect=[Response(), Response()],
            ),
            patch("infogather.sources.feedparser.parse", return_value=malformed),
            patch("infogather.sources.time.sleep"),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid feed"):
                InfoSources._fetch_feed(
                    "https://rss.arxiv.org/rss/math.NT",
                    "Number Theory",
                    attempts=2,
                    delay=0,
                )

    def test_fetch_feed_normalizes_response_header_names(self) -> None:
        body = b"""
            <rss version="2.0"><channel><item>
              <title>Result</title><guid>result-1</guid>
            </item></channel></rss>
        """
        response = Response(
            body,
            headers={"Content-Type": "application/rss+xml; charset=UTF-8"},
        )

        with patch("infogather.sources.urlopen", return_value=response):
            feed, _ = InfoSources._fetch_feed(
                "https://example.com/feed",
                "Example",
                attempts=1,
            )

        self.assertEqual(feed["entries"][0]["id"], "result-1")

    def test_fetch_feed_parses_crossref_json(self) -> None:
        body = json.dumps({
            "message": {
                "items": [{
                    "DOI": "10.4007/annals.2026.204.1.1",
                    "title": ["On K&amp;auml;hler surfaces"],
                    "author": [
                        {"given": "Jane", "family": "Doe"},
                        {"given": "John", "family": "Doe"},
                    ],
                    "published": {"date-parts": [[2026, 7, 1]]},
                    "abstract": "<jats:p>Full abstract.</jats:p>",
                    "resource": {"primary": {"URL": "https://projecteuclid.org/result"}},
                }, {
                    "DOI": "10.4007/annals.2026.204.1",
                }],
            },
        }).encode()
        response = Response(body, headers={"Content-Type": "application/json"})

        with patch("infogather.sources.urlopen", return_value=response):
            feed, _ = InfoSources._fetch_feed(
                "https://api.crossref.org/journals/0003-486X/works",
                "Annals fallback",
                attempts=1,
            )

        self.assertEqual(feed["entries"][0]["author"], "Jane Doe, John Doe")
        self.assertEqual(len(feed["entries"]), 1)
        self.assertEqual(
            feed["entries"][0]["link"],
            "https://projecteuclid.org/result",
        )
        entries = InfoSources({})._normalized_rss([{
            "source": {
                "srce_ty": "Journals",
                "key": "annals",
                "name": "Annals of Mathematics",
                "url": "https://api.crossref.org/journals/0003-486X/works",
                "selector_value": "source:Journals:annals",
            },
            "feed": feed,
        }])
        self.assertEqual(entries[0]["content"]["titl"], "On Kähler surfaces")
        self.assertEqual(entries[0]["content"]["auth"], "Jane Doe, John Doe")
        self.assertEqual(entries[0]["content"]["abst"], "Full abstract.")

    def test_fetch_feed_uses_conditional_etag(self) -> None:
        not_modified = HTTPError(
            "https://rss.arxiv.org/rss/math.NT",
            304,
            "not modified",
            {"cache-control": "max-age=120", "age": "90", "etag": "new"},
            None,
        )

        before = time.time()
        with patch(
            "infogather.sources.urlopen", side_effect=not_modified
        ) as open_url:
            feed, state = InfoSources._fetch_feed(
                "https://rss.arxiv.org/rss/math.NT",
                "Number Theory",
                {"etag": "old", "last_modified": "yesterday"},
            )

        self.assertIsNone(feed)
        self.assertEqual(state["etag"], "new")
        self.assertGreaterEqual(state["next_fetch_at"], before + 29)
        self.assertLessEqual(state["next_fetch_at"], before + 31)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("If-none-match"), "old")
        self.assertEqual(request.get_header("If-modified-since"), "yesterday")

    def test_fetch_raw_feeds_skips_fresh_cache(self) -> None:
        url = "https://rss.arxiv.org/rss/math.NT"
        source = InfoSources(
            {"arXiv": [{"name": "Number Theory", "url": url}]},
            feed_states={url: {"next_fetch_at": time.time() + 60}},
        )

        with patch.object(source, "_fetch_feed") as fetch, patch("builtins.print"):
            feeds, updates = source._fetch_raw_feeds()

        fetch.assert_not_called()
        self.assertEqual(feeds, [])
        self.assertEqual(updates, {})

    def test_fetch_results_follow_config_order_and_exclude_failed_state(self) -> None:
        urls = [f"https://example.com/{name}" for name in ("first", "bad", "last")]
        source = InfoSources({
            "arXiv": [
                {"name": "first", "url": urls[0]},
                {"name": "bad", "url": urls[1]},
            ],
            "Journals": [
                {"key": "last", "name": "last", "url": urls[2]},
            ],
        })

        def fetch(url, name, _state, attempts=2):
            if name == "bad":
                raise RuntimeError("failed")
            return {"name": name, "entries": []}, {"etag": name}

        with (
            patch.object(source, "_fetch_feed", side_effect=fetch),
            patch(
                "infogather.sources.as_completed",
                side_effect=lambda futures: reversed(list(futures)),
            ),
            patch("builtins.print"),
        ):
            feeds, updates = source._fetch_raw_feeds()

        self.assertEqual(
            [item["feed"]["name"] for item in feeds],
            ["first", "last"],
        )
        self.assertEqual(
            [item["source"]["srce_ty"] for item in feeds],
            ["arXiv", "Journals"],
        )
        self.assertEqual(feeds[1]["source"]["key"], "last")
        self.assertEqual(set(updates), {urls[0], urls[2]})
        self.assertEqual(source.failed_feeds, 1)

    def test_cache_state_is_published_only_after_normalization(self) -> None:
        source = InfoSources({})
        update = {"https://example.com/feed": {"etag": "new"}}
        with (
            patch.object(
                source,
                "_fetch_raw_feeds",
                return_value=([{
                    "source": {"srce_ty": "arXiv"},
                    "feed": {},
                }], update),
            ),
            patch.object(
                source,
                "_normalized_arXiv",
                side_effect=RuntimeError("normalization failed"),
            ),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "normalization failed"):
                source.get_normalized_feeds()

        self.assertEqual(source.feed_state_updates, {})

    def test_normalizes_mixed_arxiv_and_journal_feeds(self) -> None:
        journal = InfoSources._crossref_feed({
            "message": {
                "items": [{
                    "DOI": "10.1090/jams/1072",
                    "title": ["Journal result"],
                    "author": [
                        {"given": "Jane", "family": "Doe"},
                        {"given": "John", "family": "Doe"},
                    ],
                    "abstract": (
                        "<jats:p>Actual <mml:math alttext=\"x squared\">"
                        "<mml:mi>x</mml:mi></mml:math> abstract.</jats:p>"
                    ),
                    "published": {"date-parts": [[2026, 7, 30]]},
                    "URL": "https://doi.org/10.1090/jams/1072",
                }],
            },
        })
        raw = [
            {
                "source": {
                    "srce_ty": "arXiv",
                    "key": "math.AG",
                    "name": "Algebraic Geometry",
                    "url": "https://rss.arxiv.org/rss/math.AG",
                    "selector_value": "math.AG",
                },
                "feed": {
                    "feed": {},
                    "entries": [{
                        "id": "oai:arXiv.org:2601.00001v1",
                        "title": "arXiv result",
                        "summary": "Abstract: arXiv abstract.",
                    }],
                },
            },
            {
                "source": {
                    "srce_ty": "Journals",
                    "key": "jams",
                    "name": "Journal of the American Mathematical Society",
                    "url": "https://api.crossref.org/journals/0894-0347/works",
                    "selector_value": "source:Journals:jams",
                },
                "feed": journal,
            },
        ]
        source = InfoSources({})

        with (
            patch.object(source, "_fetch_raw_feeds", return_value=(raw, {})),
            patch("builtins.print"),
        ):
            entries = source.get_normalized_feeds()

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["srce_id"], "jams:10.1090/jams/1072")
        self.assertEqual(entries[1]["version"], 1)
        self.assertEqual(entries[1]["content"]["auth"], "Jane Doe, John Doe")
        self.assertEqual(entries[1]["content"]["abst"], "Actual x squared abstract.")
        self.assertEqual(
            entries[1]["content"]["tags"],
            ["source:Journals:jams"],
        )
        self.assertEqual(
            entries[1]["content"]["source"],
            "Journal of the American Mathematical Society",
        )

    def test_generic_rss_uses_full_content_and_ignores_annals_editor(self) -> None:
        feed = feedparser.parse(b"""
            <rss version="2.0"
              xmlns:content="http://purl.org/rss/1.0/modules/content/"
              xmlns:dc="http://purl.org/dc/elements/1.1/">
              <channel><item>
                <title>Annals result</title>
                <link>https://annals.math.princeton.edu/articles/1</link>
                <guid>annals-1</guid>
                <dc:creator>mak</dc:creator>
                <description>Short summary...</description>
                <content:encoded><![CDATA[<p>Full <b>abstract</b>.</p>]]></content:encoded>
                <category>To appear</category>
              </item></channel>
            </rss>
        """)
        source = InfoSources({})

        entries = source._normalized_rss([{
            "source": {
                "srce_ty": "Journals",
                "key": "annals",
                "name": "Annals of Mathematics",
                "url": "https://annals.math.princeton.edu/feed",
                "selector_value": "source:Journals:annals",
            },
            "feed": feed,
        }])

        self.assertEqual(entries[0]["content"]["auth"], "")
        self.assertEqual(entries[0]["content"]["abst"], "Full abstract.")
        self.assertEqual(
            entries[0]["content"]["tags"],
            ["To appear", "source:Journals:annals"],
        )

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
        self.assertEqual(latest[0]["content"]["tags"], ["math.AG", "math.DG", "math.NT"])

    def test_equal_version_deduplication_keeps_latest_timestamp(self) -> None:
        older = {
            "srce_ty": "arXiv",
            "srce_id": "2601.00001",
            "version": 1,
            "updated": "2026-03-01T00:00:00+00:00",
            "content": {"titl": "old", "tags": ["math.AG"]},
        }
        newer = {
            "srce_ty": "arXiv",
            "srce_id": "2601.00001",
            "version": 1,
            "updated": "2026-03-02T00:00:00+00:00",
            "content": {"titl": "new", "tags": ["math.NT"]},
        }

        merged = InfoSources._deduplicate_entries([older, newer])

        self.assertIs(merged[0], newer)
        self.assertEqual(merged[0]["content"]["titl"], "new")
        self.assertEqual(merged[0]["content"]["tags"], ["math.AG", "math.NT"])

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

    def test_smaxage_directive_is_honored(self) -> None:
        before = time.time()

        state = InfoSources._feed_state_from_headers(
            {"cache-control": "public, s-maxage=300"},
            previous={},
        )

        self.assertGreaterEqual(state["next_fetch_at"], before + 299)
        self.assertLessEqual(state["next_fetch_at"], before + 301)

    def test_304_without_cache_control_gets_default_revalidation(self) -> None:
        before = time.time()

        state = InfoSources._feed_state_from_headers(
            {"etag": "abc"},
            previous={},
            status_code=304,
        )

        self.assertGreaterEqual(state["next_fetch_at"], before + 1790)
        self.assertLessEqual(state["next_fetch_at"], before + 1810)

    def test_200_without_validators_drops_previous_validators(self) -> None:
        state = InfoSources._feed_state_from_headers(
            {},
            previous={"etag": "old", "last_modified": "yesterday"},
            status_code=200,
        )

        self.assertIsNone(state["etag"])
        self.assertIsNone(state["last_modified"])

    def test_304_with_no_cache_stays_immediately_stale(self) -> None:
        before = time.time()

        state = InfoSources._feed_state_from_headers(
            {"cache-control": "no-cache", "etag": "abc"},
            previous={},
            status_code=304,
        )

        self.assertLessEqual(state["next_fetch_at"], before + 1)

    def test_normalized_arxiv_uses_entry_timestamp_over_feed(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {"updated_parsed": [2026, 2, 25, 5, 0, 0, 0, 54, 0]},
                "entries": [
                    {
                        "id": "oai:arXiv.org:2601.00005v1",
                        "summary": "Abstract: Entry date wins.",
                        "published_parsed": [2026, 2, 23, 5, 0, 0, 0, 54, 0],
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["updated"], "2026-02-23T05:00:00+00:00")

    def test_normalized_arxiv_falls_back_to_feed_timestamp(self) -> None:
        source = InfoSources({})
        feeds = [
            {
                "feed": {"updated_parsed": [2026, 2, 25, 5, 0, 0, 0, 54, 0]},
                "entries": [
                    {
                        "id": "oai:arXiv.org:2601.00006v1",
                        "summary": "Abstract: Feed date fallback.",
                    }
                ],
            }
        ]

        entries = source._normalized_arXiv(feeds)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["updated"], "2026-02-25T05:00:00+00:00")

    def test_parse_arxiv_id_schemeless_host(self) -> None:
        self.assertEqual(
            InfoSources._parse_arxiv_id("arxiv.org/abs/2601.00001v1"),
            ("2601.00001", 1),
        )
        self.assertEqual(
            InfoSources._parse_arxiv_id("arxiv.org/pdf/math/0301234v2.pdf"),
            ("math/0301234", 2),
        )

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
