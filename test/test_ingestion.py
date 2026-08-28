import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from infogather.ingestion import run_ingestion
from infogather.storage import InfoStorage


class IngestionTests(unittest.TestCase):
    def test_run_ingestion_returns_counts_and_commits_entries_with_cache(self) -> None:
        class FakeSources:
            total_feeds = 2
            cached_feeds = 0
            failed_feeds = 1
            feed_state_updates = {
                "https://example.com/feed": {"etag": "new"}
            }

            def __init__(self, config, *, feed_states):
                self.config = config
                self.feed_states = feed_states

            def get_normalized_feeds(self):
                return [{
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "version": 1,
                    "favored": 0,
                    "noticed": 0,
                    "updated": "2026-03-01T00:00:00+00:00",
                    "content": {"tags": ["math.AG"]},
                }]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            db_path = root / "entries.db"
            config_path.write_text('[[arXiv]]\nname = "test"\nurl = "feed"\n')
            with (
                mock.patch("infogather.ingestion.InfoSources", FakeSources),
                redirect_stdout(io.StringIO()),
            ):
                result = run_ingestion(db_path, config_path)

            with InfoStorage.open_current(db_path) as storage:
                entries = storage.query_entries()["items"]
                states = storage.get_feed_states()

        self.assertEqual(result.total_feeds, 2)
        self.assertEqual(result.failed_feeds, 1)
        self.assertEqual(result.normalized_entries, 1)
        self.assertEqual(result.changed_entries, 1)
        self.assertEqual(entries[0]["srce_id"], "2601.00001")
        self.assertEqual(states["https://example.com/feed"]["etag"], "new")
