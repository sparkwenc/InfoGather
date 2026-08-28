import json
import math
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .filters import parse_updated


SCHEMA_VERSION = 6
BUSY_TIMEOUT_MS = 10_000
SQLITE_INT_MAX = 2 ** 63 - 1
MAX_SOURCE_TYPE_LENGTH = 512
MAX_SOURCE_ID_LENGTH = 4096
MAX_FETCH_AT = 253_402_300_799


class InfoStorage:
    # resource management
    def __init__(
        self,
        db_path: str | Path,
        *,
        _initialize: bool = True,
    ) -> None:
        raw_path = str(db_path)
        is_uri = raw_path.startswith("file:")
        uri_mode = (
            parse_qs(urlparse(raw_path).query).get("mode", [""])[0]
            if is_uri else ""
        )
        self._read_only = uri_mode == "ro"
        self._memory = raw_path == ":memory:" or uri_mode == "memory"
        if is_uri and not _initialize:
            if uri_mode not in {"ro", "rw"}:
                raise ValueError("open_current SQLite URI requires mode=ro or mode=rw")
        if raw_path == ":memory:" or is_uri:
            resolved_path = raw_path
        elif _initialize:
            path = Path(raw_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path = str(path)
        else:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"database does not exist: {path}")
            resolved_path = str(path)
        self._db_path = resolved_path
        self._conn = sqlite3.connect(
            resolved_path,
            uri=is_uri,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            schema_version = self._schema_version()
            if schema_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {schema_version} is newer than "
                    f"supported version {SCHEMA_VERSION}"
                )
            self._configure_connection(raw_path, initialize=_initialize)
            if _initialize:
                self._init_schema(schema_version)
            elif schema_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {schema_version} requires "
                    f"migration to version {SCHEMA_VERSION}"
                )
            self._validate_current_schema()
        except Exception:
            self.close()
            raise

    @classmethod
    def open_current(cls, db_path: str | Path) -> "InfoStorage":
        """Open an existing current-schema database without migrating it."""

        return cls(db_path, _initialize=False)

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
    ) -> int:
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
        return changed_cnt

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
                if entry["state_rev"] >= SQLITE_INT_MAX:
                    raise OverflowError("entry state revision is exhausted")
                conn.execute(
                    "DELETE FROM tab_entries WHERE srce_ty = ? AND srce_id = ?",
                    (srce_ty, srce_id),
                )
                entry["state_rev"] += 1
            return entry

    def restore_entry(self, entry: dict) -> int:
        """Restore a removed entry unless it has already been reinserted."""

        prepared = self._prepare_entry(entry)
        state_rev = entry["state_rev"]
        if type(state_rev) is not int or not 0 <= state_rev <= SQLITE_INT_MAX:
            raise ValueError("state revision is outside SQLite integer range")
        return self._restore_row(
            prepared["srce_ty"],
            prepared["srce_id"],
            prepared["version"],
            prepared["favored"],
            prepared["noticed"],
            state_rev,
            prepared["updated"],
            prepared["content"],
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

        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
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
            group_counts = [0] * len(groups)
            if configured_tags:
                sorted_tags = sorted(configured_tags)
                placeholders = ",".join("?" for _ in sorted_tags)
                cursor = conn.execute(
                    f"""
                    SELECT e.rowid AS entry_id, t.value AS tag
                    FROM tab_entries AS e
                    JOIN json_each(e.content, '$.tags') AS t
                    {self._where_sql([
                        *where,
                        "t.type = 'text'",
                        f"t.value IN ({placeholders})",
                    ])}
                    ORDER BY e.rowid
                    """,
                    [*params, *sorted_tags],
                )
                group_indexes = {
                    tag: [
                        index for index, group in enumerate(groups)
                        if tag in group
                    ]
                    for tag in sorted_tags
                }
                last_tag_entry: dict[str, int] = {}
                last_group_entry: list[int | None] = [None] * len(groups)
                for row in cursor:
                    entry_id = int(row["entry_id"])
                    tag = str(row["tag"])
                    if last_tag_entry.get(tag) != entry_id:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
                        last_tag_entry[tag] = entry_id
                    for index in group_indexes[tag]:
                        if last_group_entry[index] != entry_id:
                            group_counts[index] += 1
                            last_group_entry[index] = entry_id
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

    def _schema_version(self) -> int:
        return int(self._get_conn().execute("PRAGMA user_version").fetchone()[0])

    def _configure_connection(self, raw_path: str, *, initialize: bool) -> None:
        conn = self._get_conn()
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if self._read_only:
            return
        if initialize and not self._memory:
            conn.execute("PRAGMA journal_mode = WAL")
        if not self._memory:
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
        if not isinstance(entry["srce_ty"], str) or not isinstance(
            entry["srce_id"], str
        ):
            raise TypeError("entry source type and ID must be strings")
        srce_ty = entry["srce_ty"].strip()
        srce_id = entry["srce_id"].strip()
        version = entry["version"]
        favored = entry.get("favored", 0)
        noticed = entry.get("noticed", 0)
        updated = entry["updated"]
        if type(version) is not int:
            raise TypeError("entry version must be an integer")
        if type(favored) is not int or type(noticed) is not int:
            raise TypeError("entry flags must be integers")
        if not isinstance(updated, str):
            raise TypeError("entry updated timestamp must be a string")
        updated_at_us = cls._updated_at_us(updated)
        if not srce_ty or not srce_id:
            raise ValueError("entry source type and ID are required")
        if len(srce_ty) > MAX_SOURCE_TYPE_LENGTH:
            raise ValueError("entry source type is too long")
        if len(srce_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError("entry source ID is too long")
        if not 1 <= version <= SQLITE_INT_MAX:
            raise ValueError("entry version must be at least 1")
        if favored not in (0, 1) or noticed not in (0, 1):
            raise ValueError("entry flags must be 0 or 1")
        if parse_updated(updated) is None:
            raise ValueError("entry updated timestamp must be ISO 8601")
        tags = content.get("tags", [])
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) for tag in tags
        ):
            raise TypeError("entry tags must be a list of strings")
        content = dict(content)
        content["tags"] = list(dict.fromkeys(
            tag.strip() for tag in tags if tag.strip()
        ))
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
        values = []
        for url, state in states.items():
            if not isinstance(url, str) or not isinstance(state, dict):
                raise TypeError("feed state must map URL strings to objects")
            url = url.strip()
            raw_next_fetch_at = state.get("next_fetch_at", 0) or 0
            if type(raw_next_fetch_at) not in (int, float):
                raise TypeError("next fetch time must be numeric")
            next_fetch_at = float(raw_next_fetch_at)
            if not url:
                raise ValueError("feed URL is required")
            if not 0 <= next_fetch_at <= MAX_FETCH_AT or not math.isfinite(
                next_fetch_at
            ):
                raise ValueError("next fetch time is outside the supported range")
            etag = state.get("etag")
            last_modified = state.get("last_modified")
            if etag is not None and not isinstance(etag, str):
                raise TypeError("feed ETag must be a string or null")
            if last_modified is not None and not isinstance(last_modified, str):
                raise TypeError("feed Last-Modified must be a string or null")
            values.append((
                url,
                etag,
                last_modified,
                next_fetch_at,
            ))
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
            values,
        )

    def _init_schema(self, schema_version: int) -> None:
        conn = self._get_conn()
        if schema_version == SCHEMA_VERSION:
            return

        if self._read_only:
            raise RuntimeError(
                "read-only database has an outdated schema; "
                "open it read-write once to migrate"
            )

        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current_version = self._schema_version()
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current_version} is newer than "
                    f"supported version {SCHEMA_VERSION}"
                )
            if current_version == SCHEMA_VERSION:
                return
            self._install_canonical_schema()
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _validate_current_schema(self) -> None:
        conn = self._get_conn()
        expected_entries = [
            ("srce_ty", "TEXT", 1, None, 1),
            ("srce_id", "TEXT", 1, None, 2),
            ("version", "INTEGER", 1, "1", 0),
            ("favored", "INTEGER", 1, "0", 0),
            ("noticed", "INTEGER", 1, "0", 0),
            ("state_rev", "INTEGER", 1, "0", 0),
            ("updated", "TEXT", 1, None, 0),
            ("updated_at_us", "INTEGER", 1, "0", 0),
            ("content", "TEXT", 1, None, 0),
        ]
        expected_feed_state = [
            ("url", "TEXT", 1, None, 1),
            ("etag", "TEXT", 0, None, 0),
            ("last_modified", "TEXT", 0, None, 0),
            ("next_fetch_at", "REAL", 1, "0", 0),
        ]

        def columns(table: str) -> list[tuple[str, str, int, str | None, int]]:
            return [
                (
                    row["name"], row["type"], row["notnull"],
                    row["dflt_value"], row["pk"],
                )
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]

        if columns("tab_entries") != expected_entries:
            raise RuntimeError("current schema has invalid tab_entries columns")
        if columns("tab_feed_state") != expected_feed_state:
            raise RuntimeError("current schema has invalid tab_feed_state columns")

        indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(tab_entries)")
            if row["origin"] == "c"
        }
        if set(indexes) != {"idx_entries_page", "idx_entries_favored_page"}:
            raise RuntimeError("current schema has invalid tab_entries indexes")
        for name in indexes:
            indexed_columns = [
                (row["name"], row["desc"], row["coll"])
                for row in conn.execute(f"PRAGMA index_xinfo({name})")
                if row["key"] == 1
            ]
            if indexed_columns != [
                ("updated_at_us", 1, "BINARY"),
                ("srce_ty", 0, "BINARY"),
                ("srce_id", 0, "BINARY"),
            ]:
                raise RuntimeError(f"current schema has invalid index {name}")
        if indexes["idx_entries_page"]["partial"] != 0 or indexes[
            "idx_entries_favored_page"
        ]["partial"] != 1:
            raise RuntimeError("current schema has invalid index predicates")
        favored_index_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE name = 'idx_entries_favored_page'"
            ).fetchone()[0].lower().split()
        )
        if not favored_index_sql.endswith("where favored = 1"):
            raise RuntimeError("current schema has invalid favored index")

        schema_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'tab_entries'"
            ).fetchone()[0].lower().split()
        )
        required_checks = (
            f"check (length(srce_ty) between 1 and {MAX_SOURCE_TYPE_LENGTH})",
            f"check (length(srce_id) between 1 and {MAX_SOURCE_ID_LENGTH})",
            f"check (typeof(version) = 'integer' and version between 1 and {SQLITE_INT_MAX})",
            "check (typeof(favored) = 'integer' and favored in (0, 1))",
            "check (typeof(noticed) = 'integer' and noticed in (0, 1))",
            f"check (typeof(state_rev) = 'integer' and state_rev between 0 and {SQLITE_INT_MAX})",
            "check (typeof(updated_at_us) = 'integer')",
            "check (json_valid(content) and json_type(content) = 'object')",
        )
        if any(check not in schema_sql for check in required_checks):
            raise RuntimeError("current schema has invalid tab_entries checks")
        feed_sql = " ".join(
            conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'tab_feed_state'"
            ).fetchone()[0].lower().split()
        )
        if (
            "check (length(trim(url)) > 0)" not in feed_sql
            or f"and next_fetch_at between 0 and {MAX_FETCH_AT}" not in feed_sql
        ):
            raise RuntimeError("current schema has invalid tab_feed_state checks")

    def _install_canonical_schema(self) -> None:
        conn = self._get_conn()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        if "tab_entries" in tables:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tab_entries)")
            }
            required = {
                "srce_ty", "srce_id", "version", "favored",
                "updated", "content",
            }
            missing = required - columns
            if missing:
                raise RuntimeError(
                    f"cannot migrate tab_entries; missing columns: {sorted(missing)}"
                )
            conn.execute("DROP TABLE IF EXISTS tab_entries_v6")
            self._create_entries_table("tab_entries_v6")
            noticed_sql = "noticed" if "noticed" in columns else "0 AS noticed"
            state_rev_sql = (
                "state_rev" if "state_rev" in columns else "0 AS state_rev"
            )
            cursor = conn.execute(
                f"""
                SELECT srce_ty, srce_id, version, favored, {noticed_sql},
                       {state_rev_sql}, updated, content
                FROM tab_entries
                """
            )
            while rows := cursor.fetchmany(1000):
                values = []
                for row in rows:
                    identity = f"{row['srce_ty']}:{row['srce_id']}"
                    try:
                        content = json.loads(row["content"])
                        prepared = self._prepare_entry({
                            "srce_ty": row["srce_ty"],
                            "srce_id": row["srce_id"],
                            "version": row["version"],
                            "favored": row["favored"],
                            "noticed": row["noticed"],
                            "updated": row["updated"],
                            "content": content,
                        })
                        state_rev = row["state_rev"]
                        if type(state_rev) is not int or not (
                            0 <= state_rev <= SQLITE_INT_MAX
                        ):
                            raise ValueError("invalid state revision")
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"cannot migrate entry {identity}: {exc}"
                        ) from exc
                    values.append((
                        prepared["srce_ty"], prepared["srce_id"],
                        prepared["version"], prepared["favored"],
                        prepared["noticed"], state_rev, prepared["updated"],
                        prepared["updated_at_us"], prepared["content_json"],
                    ))
                conn.executemany(
                    """
                    INSERT INTO tab_entries_v6 (
                        srce_ty, srce_id, version, favored, noticed, state_rev,
                        updated, updated_at_us, content
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            conn.execute("DROP TABLE tab_entries")
            conn.execute("ALTER TABLE tab_entries_v6 RENAME TO tab_entries")
        else:
            self._create_entries_table("tab_entries")

        conn.execute("DROP TABLE IF EXISTS tab_entry_tags")
        conn.execute("DROP TABLE IF EXISTS tab_entries_fts")
        conn.execute("DROP TRIGGER IF EXISTS trg_entries_assign_pk")
        self._install_feed_state_table(tables)
        conn.execute(
            """CREATE INDEX idx_entries_page
            ON tab_entries(updated_at_us DESC, srce_ty, srce_id)"""
        )
        conn.execute(
            """CREATE INDEX idx_entries_favored_page
            ON tab_entries(updated_at_us DESC, srce_ty, srce_id)
            WHERE favored = 1"""
        )

    def _create_entries_table(self, table: str) -> None:
        self._get_conn().execute(f"""
            CREATE TABLE {table} (
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
                CHECK (length(srce_ty) BETWEEN 1 AND {MAX_SOURCE_TYPE_LENGTH}),
                CHECK (length(srce_id) BETWEEN 1 AND {MAX_SOURCE_ID_LENGTH}),
                CHECK (typeof(version) = 'integer' AND version BETWEEN 1 AND {SQLITE_INT_MAX}),
                CHECK (typeof(favored) = 'integer' AND favored IN (0, 1)),
                CHECK (typeof(noticed) = 'integer' AND noticed IN (0, 1)),
                CHECK (typeof(state_rev) = 'integer' AND state_rev BETWEEN 0 AND {SQLITE_INT_MAX}),
                CHECK (typeof(updated_at_us) = 'integer'),
                CHECK (json_valid(content) AND json_type(content) = 'object')
            )
        """)

    def _install_feed_state_table(self, tables: set[str]) -> None:
        conn = self._get_conn()
        if "tab_feed_state" not in tables:
            conn.execute(f"""
                CREATE TABLE tab_feed_state (
                    url TEXT PRIMARY KEY NOT NULL CHECK (length(trim(url)) > 0),
                    etag TEXT,
                    last_modified TEXT,
                    next_fetch_at REAL NOT NULL DEFAULT 0,
                    CHECK (
                        typeof(next_fetch_at) IN ('integer', 'real')
                        AND next_fetch_at BETWEEN 0 AND {MAX_FETCH_AT}
                    )
                )
            """)
            return
        info = list(conn.execute("PRAGMA table_info(tab_feed_state)"))
        columns = {row["name"] for row in info}
        primary_key = [
            row["name"]
            for row in sorted(info, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        ]
        if "url" not in columns or primary_key != ["url"]:
            raise RuntimeError("tab_feed_state URL must be its primary key")
        conn.execute("DROP TABLE IF EXISTS tab_feed_state_v6")
        conn.execute(f"""
            CREATE TABLE tab_feed_state_v6 (
                url TEXT PRIMARY KEY NOT NULL CHECK (length(trim(url)) > 0),
                etag TEXT,
                last_modified TEXT,
                next_fetch_at REAL NOT NULL DEFAULT 0,
                CHECK (
                    typeof(next_fetch_at) IN ('integer', 'real')
                    AND next_fetch_at BETWEEN 0 AND {MAX_FETCH_AT}
                )
            )
        """)
        etag = (
            "CASE WHEN typeof(etag) = 'text' THEN etag END"
            if "etag" in columns else "NULL"
        )
        modified = (
            "CASE WHEN typeof(last_modified) = 'text' THEN last_modified END"
            if "last_modified" in columns else "NULL"
        )
        next_fetch = "next_fetch_at" if "next_fetch_at" in columns else "0"
        conn.execute(f"""
            INSERT INTO tab_feed_state_v6 (
                url, etag, last_modified, next_fetch_at
            )
            SELECT url, {etag}, {modified},
                   CASE
                       WHEN typeof({next_fetch}) IN ('integer', 'real')
                            AND {next_fetch} BETWEEN 0 AND {MAX_FETCH_AT}
                       THEN {next_fetch}
                       ELSE 0
                   END
            FROM tab_feed_state
        """)
        conn.execute("DROP TABLE tab_feed_state")
        conn.execute("ALTER TABLE tab_feed_state_v6 RENAME TO tab_feed_state")

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
            SELECT version, state_rev, updated, updated_at_us, content
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
            if int(existing["state_rev"]) >= SQLITE_INT_MAX:
                raise OverflowError("entry state revision is exhausted")
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

        if updated_at_us < int(existing["updated_at_us"]):
            if merged_tags == existing_tags:
                return 0
            existing_content["tags"] = merged_tags
            conn.execute(
                """
                UPDATE tab_entries SET content = ?
                WHERE srce_ty = ? AND srce_id = ?
                """,
                (self._encode_content(existing_content), srce_ty, srce_id),
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
            allow_nan=False,
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
        if (
            type(expected) is not int
            or type(value) is not int
            or expected not in (0, 1)
            or value not in (0, 1)
        ):
            raise ValueError("entry flags must be 0 or 1")
        if type(expected_revision) is not int or not (
            0 <= expected_revision <= SQLITE_INT_MAX
        ):
            raise ValueError("state revision is outside SQLite integer range")
        with self._get_conn() as conn:
            cur = conn.execute(
                f"""
                UPDATE tab_entries
                SET {column} = ?, state_rev = state_rev + 1
                WHERE srce_ty = ? AND srce_id = ?
                    AND {column} = ? AND state_rev = ? AND {column} <> ?
                    AND state_rev < ?
                """,
                (
                    value, srce_ty, srce_id, expected, expected_revision,
                    value, SQLITE_INT_MAX,
                ),
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
