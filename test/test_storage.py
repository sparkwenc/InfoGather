import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from infogather.storage import InfoStorage


def _entry(
    *,
    version: int,
    noticed: int = 0,
    title: str = "title",
    srce_id: str = "2601.00001",
    updated: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    return {
        "srce_ty": "arXiv",
        "srce_id": srce_id,
        "version": version,
        "favored": 0,
        "noticed": noticed,
        "state_rev": 0,
        "updated": updated or f"2026-03-0{version}T00:00:00+00:00",
        "content": {
            "link": "https://arxiv.org/abs/2601.00001",
            "titl": title,
            "auth": "author",
            "abst": "abstract",
            "tags": tags or ["math.AG"],
        },
    }


def _items(storage: InfoStorage) -> list[dict]:
    return storage.query_entries(limit=1000)["items"]


class InfoStorageTests(unittest.TestCase):
    def test_initialization_creates_database_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "nested" / "entries.db"
            with InfoStorage(db_path):
                pass
            self.assertTrue(db_path.is_file())

    def test_initialization_supports_sqlite_file_uri(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(f"{db_path.as_uri()}?mode=rwc"):
                pass
            self.assertTrue(db_path.is_file())

    def test_current_schema_can_open_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path):
                pass

            with InfoStorage(f"{db_path.as_uri()}?mode=ro") as storage:
                self.assertEqual(_items(storage), [])

    def test_pre_feed_cache_schema_requires_writable_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tab_entries (
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        favored INTEGER NOT NULL DEFAULT 0,
                        noticed INTEGER NOT NULL DEFAULT 0,
                        state_rev INTEGER NOT NULL DEFAULT 0,
                        updated TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    );
                    CREATE INDEX idx_entries_favored ON tab_entries(favored);
                    CREATE INDEX idx_entries_noticed ON tab_entries(noticed);
                    CREATE INDEX idx_entries_updated ON tab_entries(updated);
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "read-only"):
                InfoStorage(f"{db_path.as_uri()}?mode=ro")

    def test_initialization_repairs_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path):
                pass
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP INDEX idx_entries_page")

            with InfoStorage(db_path):
                pass

            with sqlite3.connect(db_path) as conn:
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list(tab_entries)")
                }
            self.assertIn("idx_entries_page", indexes)

    def test_feed_state_url_must_be_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE tab_feed_state (
                        url TEXT,
                        variant TEXT,
                        PRIMARY KEY (url, variant)
                    )
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "primary key"):
                InfoStorage(db_path)

    def test_migrates_database_without_noticed_column(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            content = json.dumps(_entry(version=1)["content"])
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE tab_entries (
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        favored INTEGER NOT NULL DEFAULT 0,
                        updated TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tab_entries
                        (srce_ty, srce_id, version, favored, updated, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "arXiv",
                        "2601.00001",
                        1,
                        1,
                        "2026-03-01T00:00:00+00:00",
                        content,
                    ),
                )

            with InfoStorage(str(db_path)) as storage:
                entries = _items(storage)
                search = storage.query_entries(query_text="title")
                facets = storage.query_facets(
                    configured_tags={"math.AG"},
                    groups=[{"math.AG"}],
                    selected_tags=set(),
                )
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([
                        _entry(version=1, srce_id="2601.00002")
                    ])
                inserted = storage.query_entries(query_text="2601.00002")

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["favored"], 1)
            self.assertEqual(entries[0]["noticed"], 0)
            self.assertEqual(entries[0]["state_rev"], 0)
            self.assertEqual(search["total"], 1)
            self.assertEqual(facets["tag_counts"], {"math.AG": 1})
            self.assertEqual(inserted["total"], 1)

    def test_only_newer_version_replaces_stored_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(str(db_path)) as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=2, title="version 2")])
                storage.favor_entry("arXiv", "2601.00001", 1)
                storage.notice_entry_if_current("arXiv", "2601.00001", 0, 1, 1)

                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=1, title="stale")])
                stored = _items(storage)[0]
                self.assertEqual(stored["version"], 2)
                self.assertEqual(stored["content"]["titl"], "version 2")
                self.assertEqual(stored["favored"], 1)
                self.assertEqual(stored["noticed"], 1)

                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=3, title="version 3")])
                stored = _items(storage)[0]
                self.assertEqual(stored["version"], 3)
                self.assertEqual(stored["content"]["titl"], "version 3")
                self.assertEqual(stored["favored"], 1)
                self.assertEqual(stored["noticed"], 0)

    def test_insert_entries_rolls_back_whole_batch_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            valid = _entry(version=1)
            invalid = {**_entry(version=2), "content": {"bad": {1, 2}}}
            with InfoStorage(db_path) as storage:
                with self.assertRaises(TypeError), redirect_stdout(io.StringIO()):
                    storage.insert_entries([valid, invalid])
                self.assertEqual(_items(storage), [])

    def test_equal_version_merges_tags_across_batches(self) -> None:
        first = _entry(version=1)
        second = _entry(version=1)
        second["content"] = {**second["content"], "tags": ["math.NT"]}
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([first])
                storage.insert_entries([second])

            stored = _items(storage)[0]

        self.assertEqual(stored["content"]["tags"], ["math.AG", "math.NT"])

    def test_equal_version_applies_content_corrections(self) -> None:
        first = _entry(version=1, title="old title")
        corrected = _entry(version=1, title="new title")
        corrected["content"] = {
            **corrected["content"],
            "titl": "new title",
            "abst": "corrected abstract",
        }
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()) as out:
                storage.insert_entries([first])
                storage.insert_entries([corrected])
            stored = _items(storage)[0]
            self.assertIn("1/1 inserted or updated", out.getvalue())

        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["content"]["titl"], "new title")
        self.assertEqual(stored["content"]["abst"], "corrected abstract")

    def test_equal_version_unchanged_content_is_noop(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()) as out:
                storage.insert_entries([_entry(version=1)])
                storage.insert_entries([_entry(version=1)])
            self.assertIn("0/1 inserted or updated", out.getvalue())

    def test_query_entries_uses_stable_cursor_and_database_filters(self) -> None:
        entries = [
            _entry(
                version=1,
                srce_id=f"2601.{index:05d}",
                updated=f"2026-03-{index:02d}T00:00:00+00:00",
                title=f"Topic {index}",
                tags=["math.AG" if index % 2 else "math.NT"],
            )
            for index in range(1, 6)
        ]
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries(entries)
            first = storage.query_entries(limit=2)
            second = storage.query_entries(
                limit=2,
                cursor=first["next_position"],
                include_total=False,
            )
            filtered = storage.query_entries(
                selected_tags={"math.NT"},
                query_text="Topic",
                limit=10,
            )

        self.assertEqual(first["total"], 5)
        self.assertTrue(first["has_more"])
        self.assertIsNone(second["total"])
        ids = [item["srce_id"] for item in first["items"] + second["items"]]
        self.assertEqual(ids, ["2601.00005", "2601.00004", "2601.00003", "2601.00002"])
        self.assertEqual(
            [item["srce_id"] for item in filtered["items"]],
            ["2601.00004", "2601.00002"],
        )

    def test_queries_follow_update_remove_and_restore(self) -> None:
        first = _entry(version=1, title="Old searchable title")
        corrected = _entry(
            version=1,
            title="New searchable title",
            tags=["math.NT"],
        )
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([first])
                storage.insert_entries([corrected])
            self.assertEqual(
                storage.query_entries(query_text="New searchable")["total"], 1
            )
            facets = storage.query_facets(
                configured_tags={"math.AG", "math.NT"},
                groups=[{"math.AG", "math.NT"}],
                selected_tags=set(),
            )
            popped = storage.pop_entry("arXiv", "2601.00001")
            self.assertEqual(storage.query_entries(limit=10)["total"], 0)
            storage.restore_entry(popped)
            restored = storage.query_entries(query_text="New searchable")

        self.assertEqual(facets["tag_counts"], {"math.AG": 1, "math.NT": 1})
        self.assertEqual(restored["total"], 1)

    def test_short_search_does_not_match_json_field_names(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([_entry(version=1)])

            self.assertEqual(storage.query_entries(query_text="ta")["total"], 0)
            self.assertEqual(storage.query_entries(query_text="au")["total"], 1)

    def test_read_only_legacy_schema_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE tab_entries (
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        favored INTEGER NOT NULL DEFAULT 0,
                        updated TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    )
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "read-only"):
                InfoStorage(f"{db_path.as_uri()}?mode=ro")

    def test_feed_states_round_trip(self) -> None:
        with InfoStorage(":memory:") as storage:
            storage.update_feed_states(
                {
                    "https://example.com/feed": {
                        "etag": "abc",
                        "last_modified": "yesterday",
                        "next_fetch_at": 123.5,
                    }
                }
            )

            states = storage.get_feed_states()

        self.assertEqual(
            states["https://example.com/feed"],
            {
                "etag": "abc",
                "last_modified": "yesterday",
                "next_fetch_at": 123.5,
            },
        )

    def test_entry_and_feed_state_update_is_atomic(self) -> None:
        invalid = {**_entry(version=1), "content": {"bad": {1, 2}}}
        with InfoStorage(":memory:") as storage:
            with self.assertRaises(TypeError), redirect_stdout(io.StringIO()):
                storage.insert_entries(
                    [invalid],
                    {"https://example.com/feed": {"etag": "new"}},
                )

            self.assertEqual(_items(storage), [])
            self.assertEqual(storage.get_feed_states(), {})

    def test_restore_entry_restores_removed_state_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            removed = _entry(version=2, noticed=1, title="removed")
            removed["favored"] = 1
            with InfoStorage(db_path) as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([removed])
                popped = storage.pop_entry("arXiv", "2601.00001")
                expected_restored = {**removed, "state_rev": 1}
                self.assertEqual(popped, expected_restored)
                self.assertEqual(storage.restore_entry(popped), 1)
                restored = _items(storage)[0]
                self.assertEqual(restored, expected_restored)

                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=3, title="newer")])
                self.assertEqual(storage.restore_entry(removed), 0)
                restored = _items(storage)[0]
                self.assertEqual(restored["version"], 3)
                self.assertEqual(restored["content"]["titl"], "newer")
                self.assertEqual(restored["favored"], 1)
                self.assertEqual(restored["noticed"], 0)

    def test_remove_restore_invalidates_older_flag_undo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path) as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=1)])
                self.assertEqual(
                    storage.favor_entry_if_current(
                        "arXiv", "2601.00001", 0, 0, 1
                    ),
                    1,
                )
                popped = storage.pop_entry("arXiv", "2601.00001")
                self.assertEqual(storage.restore_entry(popped), 1)

                self.assertEqual(
                    storage.favor_entry_if_current(
                        "arXiv", "2601.00001", 1, 1, 0
                    ),
                    0,
                )
                restored = _items(storage)[0]
                self.assertEqual(restored["favored"], 1)
                self.assertEqual(restored["state_rev"], 2)

    def test_schema_rejects_invalid_json_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path) as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=1)])
            with sqlite3.connect(db_path) as conn, self.assertRaises(
                sqlite3.IntegrityError
            ):
                conn.execute("UPDATE tab_entries SET content = ?", ("not-json",))
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM tab_entries").fetchone()[0]
            self.assertEqual(count, 1)

    def test_export_creates_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "nested" / "feeds.md"
            with InfoStorage(":memory:") as storage:
                storage.export_entries(str(output), lambda _: True)
            self.assertTrue(output.is_file())

    def test_export_replaces_existing_file_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "feeds.md"
            output.write_text("previous", encoding="utf-8")
            with InfoStorage(":memory:") as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=1)])
                with self.assertRaisesRegex(RuntimeError, "filter failed"):
                    storage.export_entries(
                        output,
                        lambda _: (_ for _ in ()).throw(
                            RuntimeError("filter failed")
                        ),
                    )

            self.assertEqual(output.read_text(encoding="utf-8"), "previous")


if __name__ == "__main__":
    unittest.main()
