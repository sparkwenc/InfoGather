import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

from .filters import parse_updated


SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 10_000


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
        self._conn = sqlite3.connect(
            resolved_path,
            uri=is_uri,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._configure_connection(raw_path)
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
    def insert_entries(
        self,
        entries: list[dict],
        feed_states: dict[str, dict] | None = None,
    ) -> None:
        """Insert entries."""

        tot_cnt = len(entries)
        changed_cnt = 0
        prepared = [self._prepare_entry(entry) for entry in entries]
        conn = self._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for entry in prepared:
                changed_cnt += self._upsert_row(**entry)
            self._update_feed_states(feed_states or {})

        print(
            f"Insert result: {changed_cnt}/{tot_cnt} inserted or updated")

    def get_feed_states(self) -> dict[str, dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT url, etag, last_modified, next_fetch_at FROM tab_feed_state"
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
        with self._get_conn() as conn:
            self._update_feed_states(states)

    def favor_entry(self, srce_ty: str, srce_id: str, favored: int) -> int:
        """Update favored status for one entry"""

        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE tab_entries SET favored = ?, state_rev = state_rev + 1 "
                "WHERE srce_ty = ? AND srce_id = ?",
                (favored, srce_ty, srce_id),
            )
        return cur.rowcount

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

    def pop_entry(
        self,
        srce_ty: str,
        srce_id: str,
    ) -> dict | None:
        """Remove and return one entry atomically."""

        conn = self._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT
                    srce_ty, srce_id, version, favored, noticed,
                    state_rev, updated, content
                FROM tab_entries
                WHERE srce_ty = ? AND srce_id = ?
                """,
                (srce_ty, srce_id),
            ).fetchone()
            entry = None if row is None else self._row_to_entry(row)
            if entry is not None:
                conn.execute(
                    "DELETE FROM tab_entries WHERE srce_ty = ? AND srce_id = ?",
                    (srce_ty, srce_id),
                )
                entry["state_rev"] += 1
            return entry

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
            entry["content"],
        )

    def export_entries(
        self,
        filename: str | Path,
        entry_filter: Callable[[dict], bool],
    ) -> None:
        """export all entries passing the filter to a markdown file"""

        path = Path(filename).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        exported = 0
        cursor = self._get_conn().execute(
            """
            SELECT srce_ty, srce_id, version, favored, noticed,
                   state_rev, updated, content
            FROM tab_entries
            ORDER BY srce_ty, srce_id
            """
        )
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                while rows := cursor.fetchmany(1000):
                    for row in rows:
                        total += 1
                        entry = self._row_to_entry(row)
                        if not entry_filter(entry):
                            continue
                        exported += 1
                        self._write_markdown_entry(output, entry)
            temporary_path.replace(path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        print(
            f"Export result: {exported}/{total} to {filename} "
            f"with {entry_filter.__name__}"
        )

    def query_entries(
        self,
        *,
        favored: bool = False,
        unnoticed: bool = False,
        updated_since_us: int | None = None,
        updated_before_us: int | None = None,
        version_is_1: bool = False,
        version_is_not_1: bool = False,
        selected_tags: set[str] | None = None,
        query_text: str = "",
        limit: int = 30,
        cursor: tuple[int, str, str] | None = None,
        include_total: bool = True,
    ) -> dict:
        """Return one stable, database-filtered page of entries."""

        where, params = self._entry_filter_sql(
            favored=favored,
            unnoticed=unnoticed,
            updated_since_us=updated_since_us,
            updated_before_us=updated_before_us,
            version_is_1=version_is_1,
            version_is_not_1=version_is_not_1,
            selected_tags=selected_tags or set(),
            query_text=query_text,
        )
        page_where = list(where)
        page_params = list(params)
        if cursor is not None:
            updated_at_us, srce_ty, srce_id = cursor
            page_where.append(
                """(
                    e.updated_at_us < ? OR (
                        e.updated_at_us = ? AND (
                            e.srce_ty > ? OR (
                                e.srce_ty = ? AND e.srce_id > ?
                            )
                        )
                    )
                )"""
            )
            page_params.extend(
                [updated_at_us, updated_at_us, srce_ty, srce_ty, srce_id]
            )

        conn = self._get_conn()
        with conn:
            conn.execute("BEGIN")
            total = None
            if include_total:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM tab_entries AS e{self._where_sql(where)}",
                    params,
                ).fetchone()[0]
            sql = f"""
                SELECT
                    e.srce_ty, e.srce_id, e.version, e.favored,
                    e.noticed, e.state_rev, e.updated, e.updated_at_us,
                    e.content
                FROM tab_entries AS e
                {self._where_sql(page_where)}
                ORDER BY e.updated_at_us DESC, e.srce_ty, e.srce_id
                LIMIT ?
            """
            page_params.append(limit + 1)
            rows = conn.execute(sql, page_params).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_position = None
        if has_more and rows:
            last = rows[-1]
            next_position = (
                int(last["updated_at_us"]),
                str(last["srce_ty"]),
                str(last["srce_id"]),
            )
        return {
            "items": [self._row_to_entry(row) for row in rows],
            "total": total,
            "has_more": has_more,
            "next_position": next_position,
        }

    def query_facets(
        self,
        *,
        configured_tags: set[str],
        groups: list[set[str]],
        **filters: object,
    ) -> dict:
        """Count configured facets from canonical entry JSON."""

        where, params = self._entry_filter_sql(**filters)
        conn = self._get_conn()
        with conn:
            conn.execute("BEGIN")
            total = conn.execute(
                f"SELECT COUNT(*) FROM tab_entries AS e{self._where_sql(where)}",
                params,
            ).fetchone()[0]
            tag_counts: dict[str, int] = {}
            if configured_tags:
                placeholders = ",".join("?" for _ in configured_tags)
                rows = conn.execute(
                    f"""
                    SELECT t.value AS tag, COUNT(DISTINCT e.rowid) AS entry_count
                    FROM tab_entries AS e
                    JOIN json_each(e.content, '$.tags') AS t
                    {self._where_sql([
                        *where,
                        "t.type = 'text'",
                        f"t.value IN ({placeholders})",
                    ])}
                    GROUP BY t.value
                    """,
                    [*params, *sorted(configured_tags)],
                ).fetchall()
                tag_counts = {
                    str(row["tag"]): int(row["entry_count"]) for row in rows
                }
            # ponytail: repeated scans avoid duplicate indexes; add measured
            # materialized indexes only if facet latency becomes visible.
            group_counts = []
            for group_tags in groups:
                group_clause, group_params = self._selector_sql(group_tags)
                if not group_clause:
                    group_counts.append(0)
                    continue
                group_counts.append(
                    int(conn.execute(
                        "SELECT COUNT(*) FROM tab_entries AS e"
                        f"{self._where_sql([*where, group_clause])}",
                        [*params, *group_params],
                    ).fetchone()[0])
                )
        return {
            "total": int(total),
            "tag_counts": tag_counts,
            "group_counts": group_counts,
        }

    # internal methods
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("InfoStorage is closed.")
        return self._conn

    def _configure_connection(self, raw_path: str) -> None:
        conn = self._get_conn()
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if self._read_only:
            return
        if raw_path != ":memory:" and "mode=memory" not in raw_path:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")

    @staticmethod
    def _updated_at_us(value: str) -> int:
        updated = parse_updated(value)
        if updated is None:
            return 0
        return int(updated.timestamp() * 1_000_000)

    @classmethod
    def _prepare_entry(cls, entry: dict) -> dict:
        content = entry["content"]
        if not isinstance(content, dict):
            raise TypeError("entry content must be an object")
        srce_ty = str(entry["srce_ty"]).strip()
        srce_id = str(entry["srce_id"]).strip()
        version = int(entry["version"])
        favored = int(entry.get("favored", 0))
        noticed = int(entry.get("noticed", 0))
        updated = str(entry["updated"])
        updated_at_us = cls._updated_at_us(updated)
        if not srce_ty or not srce_id:
            raise ValueError("entry source type and ID are required")
        if version < 1:
            raise ValueError("entry version must be at least 1")
        if favored not in (0, 1) or noticed not in (0, 1):
            raise ValueError("entry flags must be 0 or 1")
        if parse_updated(updated) is None:
            raise ValueError("entry updated timestamp must be ISO 8601")
        return {
            "srce_ty": srce_ty,
            "srce_id": srce_id,
            "version": version,
            "favored": favored,
            "noticed": noticed,
            "updated": updated,
            "updated_at_us": updated_at_us,
            "content": content,
            "content_json": cls._encode_content(content),
        }

    def _update_feed_states(self, states: dict[str, dict]) -> None:
        if not states:
            return
        self._get_conn().executemany(
            """
            INSERT INTO tab_feed_state (
                url, etag, last_modified, next_fetch_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                next_fetch_at = excluded.next_fetch_at
            """,
            [
                (
                    url,
                    state.get("etag"),
                    state.get("last_modified"),
                    float(state.get("next_fetch_at", 0) or 0),
                )
                for url, state in states.items()
            ],
        )

    def _init_schema(self) -> None:
        conn = self._get_conn()
        required_indexes = {
            "idx_entries_identity",
            "idx_entries_page",
            "idx_entries_favored_page",
        }
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tab_entries)")
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
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        entries_ready = {
            "srce_ty", "srce_id", "noticed", "state_rev", "updated_at_us"
        }.issubset(columns) and required_indexes.issubset(indexes)
        if (
            schema_version >= SCHEMA_VERSION
            and entries_ready
            and has_feed_state
        ):
            return

        if self._read_only:
            raise RuntimeError(
                "read-only database has an outdated schema; "
                "open it read-write once to migrate"
            )

        with conn:
            conn.execute("BEGIN IMMEDIATE")
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
                    updated_at_us INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    PRIMARY KEY (srce_ty, srce_id),
                    CHECK (version >= 1),
                    CHECK (favored IN (0, 1)),
                    CHECK (noticed IN (0, 1)),
                    CHECK (state_rev >= 0),
                    CHECK (json_valid(content) AND json_type(content) = 'object')
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
            needs_backfill = schema_version < 3
            if "updated_at_us" not in columns:
                conn.execute(
                    "ALTER TABLE tab_entries "
                    "ADD COLUMN updated_at_us INTEGER NOT NULL DEFAULT 0"
                )
                needs_backfill = True
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
            conn.execute("DROP TABLE IF EXISTS tab_entry_tags")
            conn.execute("DROP TABLE IF EXISTS tab_entries_fts")
            for old_index in (
                "idx_entries_favored",
                "idx_entries_noticed",
                "idx_entries_updated", "idx_entries_pk",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {old_index}")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_identity
                ON tab_entries(srce_ty, srce_id)
                """
            )
            conn.execute("DROP TRIGGER IF EXISTS trg_entries_assign_pk")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entries_page
                ON tab_entries(updated_at_us DESC, srce_ty, srce_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entries_favored_page
                ON tab_entries(updated_at_us DESC, srce_ty, srce_id)
                WHERE favored = 1
                """
            )

            if needs_backfill:
                cursor = conn.execute(
                    "SELECT srce_ty, srce_id, updated FROM tab_entries"
                )
                while rows := cursor.fetchmany(1000):
                    conn.executemany(
                        "UPDATE tab_entries SET updated_at_us = ? "
                        "WHERE srce_ty = ? AND srce_id = ?",
                        ((self._updated_at_us(row["updated"]), row["srce_ty"],
                          row["srce_id"]) for row in rows),
                    )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _upsert_row(
        self,
        srce_ty: str,
        srce_id: str,
        version: int,
        favored: int,
        noticed: int,
        updated: str,
        updated_at_us: int,
        content: dict,
        content_json: str,
    ) -> int:
        conn = self._get_conn()
        existing = conn.execute(
            """
            SELECT version, updated, content
            FROM tab_entries
            WHERE srce_ty = ? AND srce_id = ?
            """,
            (srce_ty, srce_id),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO tab_entries (
                    srce_ty, srce_id, version,
                    favored, noticed, state_rev,
                    updated, updated_at_us, content
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    srce_ty, srce_id, version, favored, noticed,
                    updated, updated_at_us, content_json,
                ),
            )
            return 1

        stored_version = int(existing["version"])
        if version < stored_version:
            return 0

        existing_content = json.loads(existing["content"])
        existing_tags = existing_content.get("tags", []) or []
        incoming_tags = content.get("tags", []) or []
        merged_tags = list(dict.fromkeys([*existing_tags, *incoming_tags]))
        if version > stored_version:
            merged_content = dict(content)
            merged_content["tags"] = merged_tags
            conn.execute(
                """
                UPDATE tab_entries
                SET version = ?, noticed = 0, state_rev = state_rev + 1,
                    updated = ?, updated_at_us = ?, content = ?
                WHERE srce_ty = ? AND srce_id = ?
                """,
                (
                    version,
                    updated,
                    updated_at_us,
                    self._encode_content(merged_content),
                    srce_ty,
                    srce_id,
                ),
            )
            return 1

        merged_content = dict(existing_content)
        merged_content["tags"] = merged_tags
        for key, value in content.items():
            if key != "tags":
                merged_content[key] = value
        if merged_content == existing_content and updated == existing["updated"]:
            return 0
        conn.execute(
            """
            UPDATE tab_entries
            SET updated = ?, updated_at_us = ?, content = ?
            WHERE srce_ty = ? AND srce_id = ?
            """,
            (
                updated,
                updated_at_us,
                self._encode_content(merged_content),
                srce_ty,
                srce_id,
            ),
        )
        return 1

    @staticmethod
    def _encode_content(content: dict) -> str:
        return json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _where_sql(conditions: list[str]) -> str:
        return f" WHERE {' AND '.join(conditions)}" if conditions else ""

    @staticmethod
    def _selector_sql(
        selected_tags: set[str],
        *,
        alias: str = "e",
    ) -> tuple[str, list[object]]:
        if not selected_tags:
            return "", []
        placeholders = ",".join("?" for _ in selected_tags)
        return f"""EXISTS (
            SELECT 1 FROM json_each({alias}.content, '$.tags') AS selected_tag
            WHERE selected_tag.type = 'text'
              AND selected_tag.value IN ({placeholders})
        )""", sorted(selected_tags)

    @classmethod
    def _entry_filter_sql(
        cls,
        *,
        favored: bool = False,
        unnoticed: bool = False,
        updated_since_us: int | None = None,
        updated_before_us: int | None = None,
        version_is_1: bool = False,
        version_is_not_1: bool = False,
        selected_tags: set[str] | None = None,
        query_text: str = "",
    ) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        params: list[object] = []
        if favored:
            conditions.append("e.favored = 1")
        if unnoticed:
            conditions.append("e.noticed = 0")
        if updated_since_us is not None:
            conditions.append("e.updated_at_us >= ?")
            params.append(updated_since_us)
        if updated_before_us is not None:
            conditions.append("e.updated_at_us <= ?")
            params.append(updated_before_us)
        if version_is_1:
            conditions.append("e.version = 1")
        if version_is_not_1:
            conditions.append("e.version <> 1")
        selector_clause, selector_params = cls._selector_sql(selected_tags or set())
        if selector_clause:
            conditions.append(selector_clause)
            params.extend(selector_params)
        clean_query = query_text.strip().lower()
        if clean_query:
            conditions.append(
                """instr(lower(
                    e.srce_id || ' ' ||
                    coalesce(json_extract(e.content, '$.titl'), '') || ' ' ||
                    coalesce(json_extract(e.content, '$.auth'), '') || ' ' ||
                    coalesce(json_extract(e.content, '$.abst'), '') || ' ' ||
                    coalesce((
                        SELECT group_concat(value, ' ')
                        FROM json_each(e.content, '$.tags')
                        WHERE type = 'text'
                    ), '')
                ), ?) > 0"""
            )
            params.append(clean_query)
        return conditions, params

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

    def _restore_row(
        self,
        srce_ty: str,
        srce_id: str,
        version: int,
        favored: int,
        noticed: int,
        state_rev: int,
        updated: str,
        content: dict,
    ) -> int:
        conn = self._get_conn()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO tab_entries (
                    srce_ty, srce_id, version,
                    favored, noticed, state_rev,
                    updated, updated_at_us, content
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(srce_ty, srce_id) DO NOTHING
                """,
                (
                    srce_ty, srce_id, version, favored, noticed,
                    state_rev, updated, self._updated_at_us(updated),
                    self._encode_content(content),
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
    def _write_markdown_entry(output, entry: dict) -> None:
        output.write(f"## {entry['srce_ty']}:{entry['srce_id']}\n\n")
        output.write(f"- **Version:** {entry['version']}\n")
        output.write(f"- **Favored:** {bool(entry['favored'])}\n")
        output.write(f"- **Noticed:** {bool(entry.get('noticed', 0))}\n")
        output.write(f"- **Updated:** {entry['updated']}\n\n")
        output.write("- **Content**\n")
        for key, value in entry["content"].items():
            output.write(f"  - **{key}:** {value}\n")
