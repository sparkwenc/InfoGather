import json
import sqlite3
from typing import Callable


class InfoStorage:
    # resource management
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

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
    def insert_to_db(self, entries: list[dict]) -> None:
        """Insert entries."""

        tot_cnt = len(entries)
        ins_cnt = 0
        upd_cnt = 0

        for entry in entries:
            srce_ty = entry["srce_ty"]
            srce_id = entry["srce_id"]
            version = entry["version"]
            favored = entry["favored"]
            updated = entry["updated"]
            content = json.dumps(entry["content"], ensure_ascii=False)

            existing = self._fetch_one(srce_ty, srce_id)
            if existing is None:
                self._insert_row(srce_ty, srce_id,
                                 version, favored, updated, content)
                ins_cnt += 1
                continue

            if self._is_same(existing, version):
                continue

            self._update_row(srce_ty, srce_id,
                             version=version, updated=updated, content=content)
            upd_cnt += 1

        print(
            f"Insert result: {ins_cnt}/{tot_cnt} inserted, {upd_cnt}/{tot_cnt} updated")

    def favor_entry(self, srce_ty: str, srce_id: str, favored: int) -> int:
        """Update favored status for one entry"""

        return self._update_row(srce_ty, srce_id, favored=favored)

    def export_entries(self, filename: str, entry_filter: Callable[[dict], bool]) -> None:
        """export all entries passing the filter to a markdown file"""

        rows = self._fetch_all()
        exported = []
        for row in rows:
            entry = self._row_to_entry(row)
            if entry_filter(entry):
                exported.append(entry)
        self._write_to_markdown(filename, exported)

        print(
            f"Export result: {len(exported)}/{len(rows)} to {filename} with {entry_filter.__name__}")

    # internal methods
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("InfoStorage is closed.")
        return self._conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tab_entries (
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
                "CREATE INDEX IF NOT EXISTS idx_entries_favored ON tab_entries(favored)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_updated ON tab_entries(updated)"
            )

    def _fetch_one(self, srce_ty: str, srce_id: str) -> sqlite3.Row | None:
        return self._get_conn().execute(
            """
            SELECT version
            FROM tab_entries
            WHERE srce_ty = ? AND srce_id = ?
            """,
            (srce_ty, srce_id),
        ).fetchone()

    def _fetch_all(self) -> list[sqlite3.Row] | None:
        return self._get_conn().execute(
            """
            SELECT srce_ty, srce_id, version, favored, updated, content
            FROM tab_entries
            ORDER BY srce_ty, srce_id
            """
        ).fetchall()

    def _insert_row(self, srce_ty: str, srce_id: str,
                    version: int, favored: int, updated: str, content: str
                    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO tab_entries (srce_ty, srce_id, version, favored, updated, content)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (srce_ty, srce_id, version, favored, updated, content),
            )

    def _update_row(self, srce_ty: str, srce_id: str,
                    version: int | None = None,
                    favored: int | None = None,
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
        if updated is not None:
            sets.append("updated = ?")
            pars.append(updated)
        if content is not None:
            sets.append("content = ?")
            pars.append(content)

        if not sets:
            return
        pars.extend([srce_ty, srce_id])
        sql = f"UPDATE tab_entries SET {', '.join(sets)} WHERE srce_ty = ? AND srce_id = ?"
        with self._get_conn() as conn:
            cur = conn.execute(sql, pars)
        return cur.rowcount

    # helper methods
    @staticmethod
    def _is_same(existing: sqlite3.Row, version: int) -> bool:
        return existing["version"] == version

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict:
        return {
            "srce_ty": row["srce_ty"],
            "srce_id": row["srce_id"],
            "version": row["version"],
            "favored": row["favored"],
            "updated": row["updated"],
            "content": json.loads(row["content"]),
        }

    @staticmethod
    def _write_to_markdown(filename: str, exported: list[dict]) -> None:
        with open(filename, "w", encoding="utf-8") as f:
            for entry in exported:
                f.write(f"## {entry['srce_ty']}:{entry['srce_id']}\n\n")
                f.write(f"- **Version:** {entry['version']}\n")
                f.write(f"- **Favored:** {bool(entry['favored'])}\n")
                f.write(f"- **Updated:** {entry['updated']}\n\n")
                f.write(f"- **Content**\n")
                for k, v in entry["content"].items():
                    f.write(f"  - **{k}:** {v}\n")
