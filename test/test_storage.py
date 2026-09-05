import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path

from infogather.storage import InfoStorage, SCHEMA_VERSION


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


@contextmanager
def _connect(path: Path):
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection


class InfoStorageTests(unittest.TestCase):
    def test_initialization_creates_database_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "nested" / "entries.db"
            with InfoStorage(db_path):
                pass
            self.assertTrue(db_path.is_file())

    def test_file_path_containing_memory_mode_text_still_uses_wal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "mode=memory.db"
            with InfoStorage(db_path) as storage:
                journal_mode = storage._get_conn().execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
            self.assertEqual(journal_mode, "wal")

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

    def test_migration_repairs_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path):
                pass
            with _connect(db_path) as conn:
                conn.execute("DROP INDEX idx_entries_page")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

            with InfoStorage(db_path):
                pass

            with _connect(db_path) as conn:
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list(tab_entries)")
                }
            self.assertIn("idx_entries_page", indexes)

    def test_current_schema_open_validates_without_repairing_or_creating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with self.assertRaises(FileNotFoundError):
                InfoStorage.open_current(db_path)
            self.assertFalse(db_path.exists())

            with InfoStorage(db_path):
                pass
            with _connect(db_path) as conn:
                conn.execute("DROP INDEX idx_entries_page")
            with self.assertRaisesRegex(RuntimeError, "indexes"):
                InfoStorage(db_path)
            with _connect(db_path) as conn:
                indexes = {
                    row[1] for row in conn.execute("PRAGMA index_list(tab_entries)")
                }
            self.assertNotIn("idx_entries_page", indexes)

    def test_current_schema_rejects_missing_tables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            with self.assertRaisesRegex(RuntimeError, "tab_entries columns"):
                InfoStorage(db_path)

    def test_future_schema_is_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

            with self.assertRaisesRegex(RuntimeError, "newer"):
                InfoStorage(db_path)

            with _connect(db_path) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION + 1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table'"
                    ).fetchone()[0],
                    0,
                )

    def test_migration_builds_the_same_canonical_schema_as_fresh_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fresh_path = Path(td) / "fresh.db"
            legacy_path = Path(td) / "legacy.db"
            with InfoStorage(fresh_path):
                pass
            with _connect(legacy_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tab_entries (
                        entry_pk INTEGER UNIQUE,
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        favored INTEGER NOT NULL DEFAULT 0,
                        noticed INTEGER NOT NULL DEFAULT 0,
                        state_rev INTEGER NOT NULL DEFAULT 0,
                        updated TEXT NOT NULL,
                        updated_at_us INTEGER NOT NULL DEFAULT 0,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    );
                    CREATE TABLE tab_entry_tags (entry_pk INTEGER, tag TEXT);
                    CREATE TABLE tab_entries_fts (title TEXT);
                    CREATE TABLE tab_feed_state (
                        url TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        next_fetch_at REAL NOT NULL DEFAULT 0
                    );
                    PRAGMA user_version = 3;
                    """
                )
                entry = _entry(version=1)
                conn.execute(
                    """
                    INSERT INTO tab_entries (
                        entry_pk, srce_ty, srce_id, version, favored, noticed,
                        state_rev, updated, updated_at_us, content
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1, entry["srce_ty"], entry["srce_id"], entry["version"],
                        entry["favored"], entry["noticed"], entry["state_rev"],
                        entry["updated"], 0, json.dumps(entry["content"]),
                    ),
                )

            with InfoStorage(legacy_path) as storage:
                self.assertEqual(_items(storage)[0]["srce_id"], "2601.00001")

            def signature(path: Path) -> tuple:
                with _connect(path) as conn:
                    return (
                        tuple(conn.execute("PRAGMA table_info(tab_entries)")),
                        tuple(conn.execute("PRAGMA table_info(tab_feed_state)")),
                        tuple(conn.execute("PRAGMA index_list(tab_entries)")),
                    )

            self.assertEqual(signature(legacy_path), signature(fresh_path))
            with _connect(legacy_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table'"
                    )
                }
            self.assertNotIn("tab_entry_tags", tables)
            self.assertNotIn("tab_entries_fts", tables)

    def test_migrates_previous_v5_schema_with_data_and_feed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            entry = _entry(version=1)
            with _connect(db_path) as conn:
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
                        updated_at_us INTEGER NOT NULL DEFAULT 0,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id),
                        CHECK (version >= 1),
                        CHECK (favored IN (0, 1)),
                        CHECK (noticed IN (0, 1)),
                        CHECK (state_rev >= 0),
                        CHECK (json_valid(content) AND json_type(content) = 'object')
                    );
                    CREATE INDEX idx_entries_page
                    ON tab_entries(updated_at_us DESC, srce_ty, srce_id);
                    CREATE INDEX idx_entries_favored_page
                    ON tab_entries(updated_at_us DESC, srce_ty, srce_id)
                    WHERE favored = 1;
                    CREATE TABLE tab_feed_state (
                        url TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        next_fetch_at REAL NOT NULL DEFAULT 0
                    );
                    PRAGMA user_version = 5;
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tab_entries (
                        srce_ty, srce_id, version, favored, noticed, state_rev,
                        updated, updated_at_us, content
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry["srce_ty"], entry["srce_id"], entry["version"],
                        entry["favored"], entry["noticed"], entry["state_rev"],
                        entry["updated"], 0, json.dumps(entry["content"]),
                    ),
                )
                conn.execute(
                    "INSERT INTO tab_feed_state VALUES (?, ?, ?, ?)",
                    ("https://example.com/feed", "etag", "today", 123.0),
                )

            with InfoStorage(db_path) as storage:
                self.assertEqual(_items(storage)[0]["srce_id"], "2601.00001")
                self.assertEqual(
                    storage.get_feed_states()["https://example.com/feed"]["etag"],
                    "etag",
                )
                self.assertEqual(storage._schema_version(), SCHEMA_VERSION)

    def test_migration_rejects_fractional_legacy_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            entry = _entry(version=1)
            with _connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tab_entries (
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        favored INTEGER NOT NULL,
                        noticed INTEGER NOT NULL,
                        state_rev REAL NOT NULL,
                        updated TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    );
                    PRAGMA user_version = 5;
                    """
                )
                conn.execute(
                    "INSERT INTO tab_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["srce_ty"], entry["srce_id"], entry["version"],
                        0, 0, 1.9, entry["updated"],
                        json.dumps(entry["content"]),
                    ),
                )

            with self.assertRaisesRegex(RuntimeError, "invalid state revision"):
                InfoStorage(db_path)
            with _connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_failed_migration_rolls_back_schema_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tab_entries (
                        srce_ty TEXT NOT NULL,
                        srce_id TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        favored INTEGER NOT NULL DEFAULT 0,
                        updated TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY (srce_ty, srce_id)
                    );
                    INSERT INTO tab_entries VALUES (
                        'arXiv', 'bad', 1, 0,
                        '2026-03-01T00:00:00+00:00', 'not-json'
                    );
                    PRAGMA user_version = 4;
                    """
                )

            with self.assertRaisesRegex(RuntimeError, "cannot migrate entry"):
                InfoStorage(db_path)

            with _connect(db_path) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT content FROM tab_entries WHERE srce_id = 'bad'"
                    ).fetchone()[0],
                    "not-json",
                )

    def test_feed_state_url_must_be_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
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
            with _connect(db_path) as conn:
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
                storage.favor_entry_if_current(
                    "arXiv", "2601.00001", 0, 0, 1
                )
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

    def test_bulk_insert_rolls_back_database_constraint_errors(self) -> None:
        valid = _entry(version=1)
        invalid = _entry(version=1, srce_id="\0")
        with InfoStorage(":memory:") as storage:
            with self.assertRaises(sqlite3.IntegrityError):
                storage.insert_entries([valid, invalid])

            self.assertEqual(_items(storage), [])

    def test_same_batch_duplicate_preserves_upsert_semantics(self) -> None:
        first = _entry(version=1, noticed=1, tags=["math.AG"])
        first["favored"] = 1
        second = _entry(version=2, tags=["math.NT"])
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                changed = storage.insert_entries([first, second])
            stored = _items(storage)[0]

        self.assertEqual(changed, 2)
        self.assertEqual(
            (
                stored["version"], stored["favored"], stored["noticed"],
                stored["state_rev"], stored["content"]["tags"],
            ),
            (2, 1, 0, 1, ["math.AG", "math.NT"]),
        )

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

    def test_older_version_adds_cross_listing_without_replacing_current_state(self) -> None:
        current = _entry(version=2, title="Current", noticed=1)
        older = _entry(version=1, title="Stale", tags=["math.NT"])
        with InfoStorage(":memory:") as storage, redirect_stdout(io.StringIO()):
            storage.insert_entries([current])
            self.assertEqual(storage.insert_entries([older]), 1)
            stored = _items(storage)[0]
            self.assertEqual(storage.insert_entries([older]), 0)
        self.assertEqual(stored["content"]["tags"], ["math.AG", "math.NT"])
        self.assertEqual(stored["content"]["titl"], "Current")
        self.assertEqual((stored["version"], stored["noticed"], stored["state_rev"]), (2, 1, 0))
        self.assertEqual(stored["updated"], current["updated"])

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

    def test_equal_version_older_data_only_merges_tags(self) -> None:
        newer = _entry(
            version=1,
            title="new title",
            updated="2026-03-02T00:00:00+00:00",
        )
        older = _entry(
            version=1,
            title="stale title",
            updated="2026-03-01T00:00:00+00:00",
            tags=["math.NT"],
        )
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([newer])
                storage.insert_entries([older])
            stored = _items(storage)[0]

        self.assertEqual(stored["updated"], newer["updated"])
        self.assertEqual(stored["content"]["titl"], "new title")
        self.assertEqual(stored["content"]["tags"], ["math.AG", "math.NT"])

    def test_equal_version_unchanged_content_is_noop(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()) as out:
                storage.insert_entries([_entry(version=1)])
                storage.insert_entries([_entry(version=1)])
            self.assertIn("0/1 inserted or updated", out.getvalue())

    def test_storage_rejects_invalid_boundary_values(self) -> None:
        invalid_tags = _entry(version=1)
        invalid_tags["content"]["tags"] = "math.AG"
        invalid_json = _entry(version=1)
        invalid_json["content"]["score"] = float("nan")
        with InfoStorage(":memory:") as storage:
            with self.assertRaisesRegex(TypeError, "tags"):
                storage.insert_entries([invalid_tags])
            with self.assertRaisesRegex(ValueError, "JSON"):
                storage.insert_entries([invalid_json])
            with self.assertRaisesRegex(ValueError, "limit"):
                storage.query_entries(limit=0)
            with self.assertRaisesRegex(ValueError, "flags"):
                storage.favor_entry_if_current(
                    "arXiv", "2601.00001", 0, 0, 2
                )
            with self.assertRaisesRegex(ValueError, "supported range"):
                storage.insert_entries([], {
                    "https://example.com/feed": {"next_fetch_at": float("inf")}
                })

    def test_storage_rejects_lossy_type_coercion(self) -> None:
        invalid_values = [
            {"version": 1.9},
            {"favored": True},
            {"srce_id": 260100001},
            {"updated": 123},
        ]
        with InfoStorage(":memory:") as storage:
            for replacement in invalid_values:
                with self.subTest(replacement=replacement), self.assertRaises(
                    TypeError
                ):
                    storage.insert_entries([{**_entry(version=1), **replacement}])

    def test_migration_resets_invalid_feed_cache_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE tab_feed_state (
                        url TEXT PRIMARY KEY,
                        etag TEXT,
                        last_modified TEXT,
                        next_fetch_at REAL NOT NULL DEFAULT 0
                    );
                    PRAGMA user_version = 4;
                    """
                )
                conn.execute(
                    "INSERT INTO tab_feed_state VALUES (?, ?, ?, ?)",
                    ("https://example.com/feed", "etag", None, float("inf")),
                )

            with InfoStorage(db_path) as storage:
                state = storage.get_feed_states()["https://example.com/feed"]
                self.assertEqual(state["next_fetch_at"], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    storage._get_conn().execute(
                        "UPDATE tab_feed_state SET next_fetch_at = -1"
                    )

    def test_revision_exhaustion_rolls_back_ingestion_and_remove(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([_entry(version=1)])
            storage._get_conn().execute(
                "UPDATE tab_entries SET state_rev = ?",
                (2 ** 63 - 1,),
            )
            storage._get_conn().commit()
            with self.assertRaisesRegex(OverflowError, "exhausted"):
                storage.insert_entries(
                    [_entry(version=2)],
                    {"https://example.com/feed": {"etag": "new"}},
                )
            self.assertEqual(storage.get_feed_states(), {})
            with self.assertRaisesRegex(OverflowError, "exhausted"):
                storage.pop_entry("arXiv", "2601.00001")
            self.assertEqual(_items(storage)[0]["version"], 1)

    def test_open_current_uri_cannot_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "missing.db"
            with self.assertRaisesRegex(ValueError, "mode=ro or mode=rw"):
                InfoStorage.open_current(f"{db_path.as_uri()}?mode=rwc")
            self.assertFalse(db_path.exists())

    def test_noop_flag_update_does_not_increment_revision(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([_entry(version=1)])
            self.assertEqual(
                storage.favor_entry_if_current(
                    "arXiv", "2601.00001", 0, 0, 0
                ),
                0,
            )
            stored = _items(storage)[0]
        self.assertEqual(stored["state_rev"], 0)

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
        self.assertEqual(facets["group_counts"], [1])
        self.assertEqual(restored["total"], 1)

    def test_facets_count_entries_without_configured_tags(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([
                    _entry(version=1, srce_id="2601.00001", tags=["math.AG"]),
                    _entry(version=1, srce_id="2601.00002", tags=["math.OTHER"]),
                ])
            facets = storage.query_facets(
                configured_tags={"math.AG"},
                groups=[{"math.AG"}],
                query_text="title",
            )

        self.assertEqual(facets, {
            "total": 2,
            "tag_counts": {"math.AG": 1},
            "group_counts": [1],
        })

    def test_short_search_does_not_match_json_field_names(self) -> None:
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([_entry(version=1)])

            self.assertEqual(storage.query_entries(query_text="ta")["total"], 0)
            self.assertEqual(storage.query_entries(query_text="au")["total"], 1)

    def test_search_prefilter_preserves_exact_search_semantics(self) -> None:
        entry = _entry(
            version=1,
            title='Mixed CASE 100%_ "quoted" \\ path\nnext',
            tags=["math.AG"],
        )
        entry["content"]["auth"] = "Distinct Author"
        entry["content"]["abst"] = "Unique abstract"
        with InfoStorage(":memory:") as storage:
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([entry])
            results = {
                query: storage.query_entries(query_text=query)["total"]
                for query in (
                    "mixed", "CASE", "100%_", '"quoted"', "\\ path",
                    "path\nnext", "distinct", "unique", "math.ag",
                    "2601.00001", "arxiv.org",
                )
            }

        self.assertEqual(results, {
            "mixed": 1,
            "CASE": 1,
            "100%_": 1,
            '"quoted"': 1,
            "\\ path": 1,
            "path\nnext": 1,
            "distinct": 1,
            "unique": 1,
            "math.ag": 1,
            "2601.00001": 1,
            "arxiv.org": 0,
        })

    def test_search_casefolds_unicode_in_entries_and_facets(self) -> None:
        entry = _entry(version=1, title="KÄHLER Straße Σ", srce_id="ÉTUDE")
        with InfoStorage(":memory:") as storage, redirect_stdout(io.StringIO()):
            storage.insert_entries([entry])
            for query in ("kähler", "KÄHLER", "STRASSE", "σ", "ς", "étude"):
                with self.subTest(query=query):
                    self.assertEqual(storage.query_entries(query_text=query)["total"], 1)
                    facets = storage.query_facets(
                        configured_tags={"math.AG"}, groups=[{"math.AG"}], query_text=query
                    )
                    self.assertEqual(facets["tag_counts"], {"math.AG": 1})

    def test_read_only_legacy_schema_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with _connect(db_path) as conn:
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
            with redirect_stdout(io.StringIO()):
                storage.insert_entries([], {
                    "https://example.com/feed": {
                        "etag": "abc",
                        "last_modified": "yesterday",
                        "next_fetch_at": 123.5,
                    }
                })

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
            with _connect(db_path) as conn, self.assertRaises(
                sqlite3.IntegrityError
            ):
                conn.execute("UPDATE tab_entries SET content = ?", ("not-json",))
            with _connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM tab_entries").fetchone()[0]
            self.assertEqual(count, 1)
