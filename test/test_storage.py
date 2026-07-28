import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from infogather.storage import InfoStorage


def _entry(*, version: int, noticed: int = 0, title: str = "title") -> dict:
    return {
        "srce_ty": "arXiv",
        "srce_id": "2601.00001",
        "version": version,
        "favored": 0,
        "noticed": noticed,
        "updated": f"2026-03-0{version}T00:00:00+00:00",
        "content": {
            "link": "https://arxiv.org/abs/2601.00001",
            "titl": title,
            "auth": "author",
            "abst": "abstract",
            "tags": ["math.AG"],
        },
    }


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
                self.assertEqual(storage.export_entries_json(), [])

    def test_initialization_repairs_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(db_path):
                pass
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP INDEX idx_entries_noticed")

            with InfoStorage(db_path):
                pass

            with sqlite3.connect(db_path) as conn:
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list(tab_entries)")
                }
            self.assertIn("idx_entries_noticed", indexes)

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
                entries = storage.export_entries_json()

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["favored"], 1)
            self.assertEqual(entries[0]["noticed"], 0)

    def test_only_newer_version_replaces_stored_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            with InfoStorage(str(db_path)) as storage:
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=2, title="version 2")])
                storage.favor_entry("arXiv", "2601.00001", 1)
                storage.notice_entry("arXiv", "2601.00001", 1)

                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=1, title="stale")])
                stored = storage.export_entries_json()[0]
                self.assertEqual(stored["version"], 2)
                self.assertEqual(stored["content"]["titl"], "version 2")
                self.assertEqual(stored["favored"], 1)
                self.assertEqual(stored["noticed"], 1)

                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([_entry(version=3, title="version 3")])
                stored = storage.export_entries_json()[0]
                self.assertEqual(stored["version"], 3)
                self.assertEqual(stored["content"]["titl"], "version 3")
                self.assertEqual(stored["favored"], 1)
                self.assertEqual(stored["noticed"], 0)

    def test_export_creates_output_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "nested" / "feeds.md"
            with InfoStorage(":memory:") as storage:
                storage.export_entries(str(output), lambda _: True)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
