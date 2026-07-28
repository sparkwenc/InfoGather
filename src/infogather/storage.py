import json
import sqlite3
from pathlib import Path
from typing import Callable


class InfoStorage:
    # resource management
    def __init__(self, db_path: str | Path) -> None:
        raw_path = str(db_path)
        is_uri = raw_path.startswith("file:")
        if raw_path == ":memory:" or is_uri:
            resolved_path = raw_path
        else:
            path = Path(raw_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path = str(path)
        self._db_path = resolved_path
        self._conn = sqlite3.connect(resolved_path, uri=is_uri)
        self._conn.row_factory = sqlite3.Row
        try:
            self._init_schema()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def __enter__(self) -> "InfoStorage":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    # main methods
    def insert_entries(self, entries: list[dict]) -> None:
        """Insert entries."""

        tot_cnt = len(entries)
        changed_cnt = 0

        for entry in entries:
            srce_ty = entry["srce_ty"]
            srce_id = entry["srce_id"]
            version = entry["version"]
            favored = entry["favored"]
            noticed = int(entry.get("noticed", 0))
            updated = entry["updated"]
            content = json.dumps(entry["content"], ensure_ascii=False)

            changed = self._upsert_row(
                srce_ty,
                srce_id,
                version,
                favored,
                noticed,
                updated,
                content,
            )
            changed_cnt += changed

        print(
            f"Insert result: {changed_cnt}/{tot_cnt} inserted or updated")

    def favor_entry(self, srce_ty: str, srce_id: str, favored: int) -> int:
        """Update favored status for one entry"""

        return self._update_row(srce_ty, srce_id, favored=favored)

    def notice_entry(self, srce_ty: str, srce_id: str, noticed: int) -> int:
        """Update noticed status for one entry"""

        return self._update_row(srce_ty, srce_id, noticed=noticed)

    def remove_entry(self, srce_ty: str, srce_id: str) -> int:
        """Remove one entry"""

        return self._delete_row(srce_ty, srce_id)

    def export_entries(
        self,
        filename: str | Path,
        entry_filter: Callable[[dict], bool],
    ) -> None:
        """export all entries passing the filter to a markdown file"""

        entries = self.export_entries_json()
        exported = [entry for entry in entries if entry_filter(entry)]
        self._write_to_markdown(filename, exported)

        print(
            f"Export result: {len(exported)}/{len(entries)} to {filename} with {entry_filter.__name__}")

    def export_entries_json(
        self,
        entry_filter: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Export entries as JSON-friendly dictionaries for read-only interfaces."""

        entries = [self._row_to_entry(row) for row in self._fetch_all()]
        if entry_filter is None:
            return entries
        return [entry for entry in entries if entry_filter(entry)]

    # internal methods
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("InfoStorage is closed.")
        return self._conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        required_indexes = {
            "idx_entries_favored",
            "idx_entries_noticed",
            "idx_entries_updated",
        }
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tab_entries)")
        }
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(tab_entries)")
        }
        if "noticed" in columns and required_indexes.issubset(indexes):
            return

        try:
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tab_entries)")
            }
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tab_entries (
                    srce_ty TEXT NOT NULL,
                    srce_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    favored INTEGER NOT NULL DEFAULT 0,
                    noticed INTEGER NOT NULL DEFAULT 0,
                    updated TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (srce_ty, srce_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tab_entries)")
            }
            if "noticed" not in columns:
                conn.execute(
                    "ALTER TABLE tab_entries "
                    "ADD COLUMN noticed INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_favored ON tab_entries(favored)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_noticed ON tab_entries(noticed)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_updated ON tab_entries(updated)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _fetch_all(self) -> list[sqlite3.Row]:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT srce_ty, srce_id, version, favored, noticed, updated, content
                FROM tab_entries
                ORDER BY srce_ty, srce_id
                """
            )
        return cur.fetchall()

    def _upsert_row(self, srce_ty: str, srce_id: str,
                    version: int, favored: int, noticed: int, updated: str, content: str
                    ) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO tab_entries (srce_ty, srce_id, version, favored, noticed, updated, content)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(srce_ty, srce_id) DO UPDATE SET
                    version = excluded.version,
                    noticed = 0,
                    updated = excluded.updated,
                    content = excluded.content
                WHERE excluded.version > tab_entries.version
                """,
                (srce_ty, srce_id, version, favored, noticed, updated, content),
            )
        return cur.rowcount

    def _update_row(self, srce_ty: str, srce_id: str,
                    version: int | None = None,
                    favored: int | None = None,
                    noticed: int | None = None,
                    updated: str | None = None,
                    content: str | None = None,
                    ) -> int:
        sets = []
        pars = []
        if version is not None:
            sets.append("version = ?")
            pars.append(version)
        if favored is not None:
            sets.append("favored = ?")
            pars.append(favored)
        if noticed is not None:
            sets.append("noticed = ?")
            pars.append(noticed)
        if updated is not None:
            sets.append("updated = ?")
            pars.append(updated)
        if content is not None:
            sets.append("content = ?")
            pars.append(content)

        if not sets:
            return 0
        pars.extend([srce_ty, srce_id])
        sql = f"UPDATE tab_entries SET {', '.join(sets)} WHERE srce_ty = ? AND srce_id = ?"
        with self._get_conn() as conn:
            cur = conn.execute(sql, pars)
        return cur.rowcount

    def _delete_row(self, srce_ty: str, srce_id: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                DELETE FROM tab_entries
                WHERE srce_ty = ? AND srce_id = ?
                """,
                (srce_ty, srce_id),
            )
        return cur.rowcount

    # helper methods
    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict:
        return {
            "srce_ty": row["srce_ty"],
            "srce_id": row["srce_id"],
            "version": row["version"],
            "favored": row["favored"],
            "noticed": row["noticed"],
            "updated": row["updated"],
            "content": json.loads(row["content"]),
        }

    @staticmethod
    def _write_to_markdown(filename: str | Path, exported: list[dict]) -> None:
        path = Path(filename).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for entry in exported:
                f.write(f"## {entry['srce_ty']}:{entry['srce_id']}\n\n")
                f.write(f"- **Version:** {entry['version']}\n")
                f.write(f"- **Favored:** {bool(entry['favored'])}\n")
                f.write(f"- **Noticed:** {bool(entry.get('noticed', 0))}\n")
                f.write(f"- **Updated:** {entry['updated']}\n\n")
                f.write(f"- **Content**\n")
                for k, v in entry["content"].items():
                    f.write(f"  - **{k}:** {v}\n")
