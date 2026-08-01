import json
import sqlite3
from pathlib import Path
from typing import Callable


class InfoStorage:
    # resource management
    def __init__(self, db_path: str | Path) -> None:
        raw_path = str(db_path)
        is_uri = raw_path.startswith("file:")
        self._read_only = is_uri and "mode=ro" in raw_path
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
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for entry in entries:
                changed_cnt += self._upsert_row(
                    entry["srce_ty"],
                    entry["srce_id"],
                    entry["version"],
                    entry["favored"],
                    int(entry.get("noticed", 0)),
                    entry["updated"],
                    json.dumps(entry["content"], ensure_ascii=False),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        print(
            f"Insert result: {changed_cnt}/{tot_cnt} inserted or updated")

    def get_feed_states(self) -> dict[str, dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT url, etag, last_modified, next_fetch_at
                FROM tab_feed_state
                """
            ).fetchall()
        return {
            row["url"]: {
                "etag": row["etag"],
                "last_modified": row["last_modified"],
                "next_fetch_at": row["next_fetch_at"],
            }
            for row in rows
        }

    def update_feed_states(self, states: dict[str, dict]) -> None:
        if not states:
            return
        values = [
            (
                url,
                state.get("etag"),
                state.get("last_modified"),
                float(state.get("next_fetch_at", 0) or 0),
            )
            for url, state in states.items()
        ]
        with self._get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO tab_feed_state (
                    url, etag, last_modified, next_fetch_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    next_fetch_at = excluded.next_fetch_at
                """,
                values,
            )

    def favor_entry(self, srce_ty: str, srce_id: str, favored: int) -> int:
        """Update favored status for one entry"""

        return self._update_row(srce_ty, srce_id, favored=favored)

    def notice_entry(self, srce_ty: str, srce_id: str, noticed: int) -> int:
        """Update noticed status for one entry"""

        return self._update_row(srce_ty, srce_id, noticed=noticed)

    def favor_entry_if_current(
        self,
        srce_ty: str,
        srce_id: str,
        expected: int,
        expected_revision: int,
        favored: int,
    ) -> int:
        return self._update_flag_if_current(
            srce_ty, srce_id, "favored", expected, expected_revision, favored
        )

    def notice_entry_if_current(
        self,
        srce_ty: str,
        srce_id: str,
        expected: int,
        expected_revision: int,
        noticed: int,
    ) -> int:
        return self._update_flag_if_current(
            srce_ty, srce_id, "noticed", expected, expected_revision, noticed
        )

    def remove_entry(self, srce_ty: str, srce_id: str) -> int:
        """Remove one entry"""

        return self._delete_row(srce_ty, srce_id)

    def pop_entry(
        self,
        srce_ty: str,
        srce_id: str,
        *,
        clear_feed_states: bool = False,
    ) -> dict | None:
        """Remove and return one entry atomically."""

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                DELETE FROM tab_entries
                WHERE srce_ty = ? AND srce_id = ?
                RETURNING
                    srce_ty, srce_id, version, favored, noticed,
                    state_rev, updated, content
                """,
                (srce_ty, srce_id),
            )
            row = cur.fetchone()
            entry = None if row is None else self._row_to_entry(row)
            if entry is not None:
                entry["state_rev"] += 1
                if clear_feed_states:
                    conn.execute("DELETE FROM tab_feed_state")
            conn.commit()
            return entry
        except Exception:
            conn.rollback()
            raise

    def restore_entry(self, entry: dict) -> int:
        """Restore a removed entry unless it has already been reinserted."""

        return self._restore_row(
            entry["srce_ty"],
            entry["srce_id"],
            entry["version"],
            entry["favored"],
            entry["noticed"],
            entry["state_rev"],
            entry["updated"],
            json.dumps(entry["content"], ensure_ascii=False),
        )

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
        feed_state_info = list(conn.execute("PRAGMA table_info(tab_feed_state)"))
        feed_state_columns = {row["name"] for row in feed_state_info}
        feed_state_primary_key = [
            row["name"]
            for row in sorted(feed_state_info, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        ]
        feed_state_url_is_key = feed_state_primary_key == ["url"]
        has_feed_state = {
            "url", "etag", "last_modified", "next_fetch_at"
        }.issubset(feed_state_columns) and feed_state_url_is_key
        entries_ready = (
            {"noticed", "state_rev"}.issubset(columns)
            and required_indexes.issubset(indexes)
        )
        if (
            entries_ready
            and (has_feed_state or self._read_only)
        ):
            return

        if self._read_only:
            raise RuntimeError(
                "read-only database has an outdated schema; "
                "open it read-write once to migrate"
            )

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
                    state_rev INTEGER NOT NULL DEFAULT 0,
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
            if "state_rev" not in columns:
                conn.execute(
                    "ALTER TABLE tab_entries "
                    "ADD COLUMN state_rev INTEGER NOT NULL DEFAULT 0"
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tab_feed_state (
                    url TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    next_fetch_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            feed_state_info = list(
                conn.execute("PRAGMA table_info(tab_feed_state)")
            )
            feed_state_columns = {row["name"] for row in feed_state_info}
            primary_key = [
                row["name"]
                for row in sorted(feed_state_info, key=lambda row: int(row["pk"]))
                if int(row["pk"]) > 0
            ]
            url_is_key = primary_key == ["url"]
            if not url_is_key:
                raise RuntimeError("tab_feed_state URL must be its primary key")
            for column, definition in (
                ("etag", "TEXT"),
                ("last_modified", "TEXT"),
                ("next_fetch_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in feed_state_columns:
                    conn.execute(
                        f"ALTER TABLE tab_feed_state "
                        f"ADD COLUMN {column} {definition}"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _fetch_all(self) -> list[sqlite3.Row]:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT srce_ty, srce_id, version, favored, noticed, state_rev, updated, content
                FROM tab_entries
                ORDER BY srce_ty, srce_id
                """
            )
        return cur.fetchall()

    def _upsert_row(self, srce_ty: str, srce_id: str,
                    version: int, favored: int, noticed: int, updated: str, content: str
                    ) -> int:
        cur = self._get_conn().execute(
            """
            INSERT INTO tab_entries (
                srce_ty, srce_id, version, favored, noticed, state_rev, updated, content
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(srce_ty, srce_id) DO UPDATE SET
                version = excluded.version,
                noticed = 0,
                state_rev = tab_entries.state_rev + 1,
                updated = excluded.updated,
                content = excluded.content
            WHERE excluded.version > tab_entries.version
            """,
            (srce_ty, srce_id, version, favored, noticed, updated, content),
        )
        if cur.rowcount:
            return cur.rowcount

        existing = self._get_conn().execute(
            """
            SELECT version, content FROM tab_entries
            WHERE srce_ty = ? AND srce_id = ?
            """,
            (srce_ty, srce_id),
        ).fetchone()
        if existing is None or existing["version"] != version:
            return 0
        existing_content = json.loads(existing["content"])
        incoming_content = json.loads(content)
        existing_tags = existing_content.get("tags", []) or []
        incoming_tags = incoming_content.get("tags", []) or []
        merged_tags = list(dict.fromkeys([*existing_tags, *incoming_tags]))
        changed = False
        if merged_tags != existing_tags:
            existing_content["tags"] = merged_tags
            changed = True
        for key, value in incoming_content.items():
            if key == "tags":
                continue
            if existing_content.get(key) != value:
                existing_content[key] = value
                changed = True
        if not changed:
            return 0
        updated_row = self._get_conn().execute(
            """
            UPDATE tab_entries SET content = ?
            WHERE srce_ty = ? AND srce_id = ? AND version = ?
            """,
            (
                json.dumps(existing_content, ensure_ascii=False),
                srce_ty,
                srce_id,
                version,
            ),
        )
        return updated_row.rowcount

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
        if favored is not None or noticed is not None:
            sets.append("state_rev = state_rev + 1")

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

    def _update_flag_if_current(
        self,
        srce_ty: str,
        srce_id: str,
        column: str,
        expected: int,
        expected_revision: int,
        value: int,
    ) -> int:
        if column not in {"favored", "noticed"}:
            raise ValueError(f"unsupported flag column: {column}")
        with self._get_conn() as conn:
            cur = conn.execute(
                f"""
                UPDATE tab_entries
                SET {column} = ?, state_rev = state_rev + 1
                WHERE srce_ty = ? AND srce_id = ?
                    AND {column} = ? AND state_rev = ?
                """,
                (value, srce_ty, srce_id, expected, expected_revision),
            )
        return cur.rowcount

    def _restore_row(self, srce_ty: str, srce_id: str,
                     version: int, favored: int, noticed: int, state_rev: int,
                     updated: str, content: str
                     ) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO tab_entries (
                    srce_ty, srce_id, version, favored, noticed, state_rev, updated, content
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(srce_ty, srce_id) DO NOTHING
                """,
                (
                    srce_ty, srce_id, version, favored, noticed,
                    state_rev, updated, content,
                ),
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
            "state_rev": row["state_rev"],
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
