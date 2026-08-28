import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from infogather.ingestion import _ingestion_lock, run_ingestion
from infogather.storage import InfoStorage


class IngestionTests(unittest.TestCase):
    def test_file_path_containing_memory_mode_text_uses_file_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "mode=memory.db"
            with _ingestion_lock(db_path):
                pass
            self.assertTrue(Path(f"{db_path}.ingest.lock").is_file())

    def test_named_shared_memory_ingestion_lock_serializes_threads(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        uri = "file:ingestion-lock?cache=shared&mode=memory"

        def hold_first():
            with _ingestion_lock(uri):
                first_entered.set()
                release_first.wait(2)

        def enter_second():
            with _ingestion_lock("file:ingestion-lock?mode=memory&cache=shared"):
                second_entered.set()

        first = threading.Thread(target=hold_first)
        second = threading.Thread(target=enter_second)
        first.start()
        self.assertTrue(first_entered.wait(2))
        second.start()
        try:
            self.assertFalse(second_entered.wait(0.1))
        finally:
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertTrue(second_entered.is_set())

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

    def test_run_ingestion_serializes_runs_for_the_same_database(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        calls_lock = threading.Lock()
        calls = 0

        class BlockingSources:
            def __init__(self, _config, *, feed_states):
                nonlocal calls
                self.total_feeds = 0
                self.cached_feeds = 0
                self.failed_feeds = 0
                self.feed_state_updates = {}
                with calls_lock:
                    calls += 1
                    self.call = calls

            def get_normalized_feeds(self):
                if self.call == 1:
                    first_entered.set()
                    release_first.wait(2)
                else:
                    second_entered.set()
                return []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            db_path = root / "entries.db"
            config_path.write_text("")
            errors = []

            def run():
                try:
                    run_ingestion(db_path, config_path)
                except Exception as exc:
                    errors.append(exc)

            with (
                mock.patch("infogather.ingestion.InfoSources", BlockingSources),
                mock.patch("builtins.print"),
            ):
                first = threading.Thread(target=run)
                second = threading.Thread(target=run)
                first.start()
                self.assertTrue(first_entered.wait(2))
                second.start()
                try:
                    self.assertFalse(second_entered.wait(0.1))
                finally:
                    release_first.set()
                    first.join(2)
                    second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(errors, [])
