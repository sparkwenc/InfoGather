from .storage import (
    InfoStorage,
    MAX_SOURCE_ID_LENGTH,
    MAX_SOURCE_TYPE_LENGTH,
    SQLITE_INT_MAX,
)
from .paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, WEB_DIR
from .ingestion import load_config, run_ingestion
from .sources import configured_sources

import argparse
import base64
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INS_LOCK = threading.Lock()
INS_JOB = {
    "state": "idle",  # idle | running | succeeded | failed
    "progress": 0,
    "message": "就绪",
    "started_at": None,
    "ended_at": None,
}
REMOVE_UNDO_LOCK = threading.Lock()
REMOVE_UNDO: dict[str, dict] = {}
MAX_REMOVE_UNDOS = 100
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CURSOR_BYTES = 16_384
MAX_QUERY_FIELDS = 200
MAX_QUERY_TEXT_LENGTH = 500
MAX_SELECTED_TAGS = 100
MAX_TAG_LENGTH = 256
SQLITE_INT_MIN = -(2 ** 63)


def _parse_int(raw: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, min_value), max_value)


def _parse_flag(raw: str) -> bool:
    return raw.strip() == "1"


def _parse_selectors(values: list[str]) -> set[str]:
    tag_values: set[str] = set()
    for value in values:
        for part in re.split(r",\s*(?=tag:)", value):
            selector = part.strip()
            if not selector:
                continue
            if selector.startswith("tag:"):
                tag = selector[4:].strip()
                if tag:
                    if len(tag) > MAX_TAG_LENGTH:
                        raise ValueError("tag selector is too long")
                    tag_values.add(tag)
                    if len(tag_values) > MAX_SELECTED_TAGS:
                        raise ValueError("too many tag selectors")
                continue
    return tag_values


def _encode_cursor(position: tuple[int, str, str] | None) -> str | None:
    if position is None:
        return None
    _validate_cursor(position)
    payload = json.dumps(position, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _validate_cursor(value: object) -> tuple[int, str, str]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or isinstance(value[0], bool)
        or not isinstance(value[0], int)
        or not SQLITE_INT_MIN <= value[0] <= SQLITE_INT_MAX
        or not isinstance(value[1], str)
        or len(value[1]) > MAX_SOURCE_TYPE_LENGTH
        or not isinstance(value[2], str)
        or len(value[2]) > MAX_SOURCE_ID_LENGTH
    ):
        raise ValueError("invalid cursor")
    return value[0], value[1], value[2]


def _decode_cursor(raw: str) -> tuple[int, str, str] | None:
    if not raw:
        return None
    if len(raw) > MAX_CURSOR_BYTES:
        raise ValueError("cursor is too long")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid cursor") from exc
    return _validate_cursor(value)


def _entry_query_options(query: dict[str, list[str]]) -> dict:
    updated_within_day = _parse_flag(
        query.get("updated_within_day", [""])[0]
    )
    updated_within_week = _parse_flag(
        query.get("updated_within_week", [""])[0]
    )
    selected_tags = _parse_selectors(query.get("selectors", []))
    query_text = query.get("q", [""])[0].strip()
    if len(query_text) > MAX_QUERY_TEXT_LENGTH:
        raise ValueError("query is too long")
    now = datetime.now(timezone.utc)
    window = None
    if updated_within_day:
        window = timedelta(days=1)
    elif updated_within_week:
        window = timedelta(days=7)
    return {
        "favored": _parse_flag(query.get("favored", [""])[0]),
        "unnoticed": _parse_flag(query.get("unnoticed", [""])[0]),
        "updated_since_us": (
            int((now - window).timestamp() * 1_000_000) if window else None
        ),
        "updated_before_us": (
            int(now.timestamp() * 1_000_000) if window else None
        ),
        "version_is_1": _parse_flag(query.get("version_is_1", [""])[0]),
        "version_is_not_1": _parse_flag(
            query.get("version_is_not_1", [""])[0]
        ),
        "selected_tags": selected_tags,
        "query_text": query_text,
    }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ins_snapshot_unlocked() -> dict:
    return dict(INS_JOB)


def _ins_snapshot() -> dict:
    with INS_LOCK:
        return _ins_snapshot_unlocked()


def _ins_update(**kwargs: object) -> None:
    with INS_LOCK:
        INS_JOB.update(kwargs)


def _ins_report(progress: int, message: str) -> None:
    with INS_LOCK:
        INS_JOB["progress"] = max(int(INS_JOB["progress"]), progress)
        INS_JOB["message"] = message


def _run_ins_job(db_path: str | Path, conf_path: Path) -> None:
    try:
        result = run_ingestion(db_path, conf_path, progress=_ins_report)
        message = f"写入 {result.changed_entries}/{result.normalized_entries} 条"
        _ins_report(100, message)
        _ins_update(
            state="succeeded",
            ended_at=_utcnow_iso(),
        )
    except Exception as exc:
        message = f"拉取失败: {exc}"
        _ins_report(max(int(_ins_snapshot()["progress"]), 1), message)
        _ins_update(
            state="failed",
            ended_at=_utcnow_iso(),
        )


def _load_configured_sources(conf_path: Path) -> list[dict]:
    if not conf_path.exists():
        return []
    sources = configured_sources(load_config(conf_path))
    groups = []
    by_name = {}
    for source in sources:
        group = by_name.get(source["srce_ty"])
        if group is None:
            group = {"name": source["srce_ty"], "children": []}
            by_name[source["srce_ty"]] = group
            groups.append(group)
        group["children"].append({
            "name": source["name"],
            "selector_value": source["selector_value"],
        })
    return groups


def _normalize_db_path(raw_path: str) -> str | Path:
    if raw_path.startswith("file:"):
        mode = parse_qs(urlparse(raw_path).query).get("mode", [""])[0]
        if mode not in {"ro", "rw"}:
            raise ValueError("SQLite URI mode must be ro or rw")
        return raw_path
    return Path(raw_path).expanduser().resolve()


def _is_loopback_host(host: str) -> bool:
    host = host.strip()
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _http_authority(value: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname.casefold(), port or 80


class InfoHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exception(), ConnectionResetError):
            return
        super().handle_error(request, client_address)


class ThreadingHTTPServerV6(InfoHTTPServer):
    address_family = socket.AF_INET6


class InfoHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def __init__(
        self,
        *args,
        db_path: str | Path,
        conf_path: Path,
        **kwargs,
    ) -> None:
        self._db_path = db_path
        self._conf_path = conf_path
        self._storage: InfoStorage | None = None
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._storage is not None:
                self._storage.close()

    @contextmanager
    def _current_storage(self):
        if not hasattr(self, "_storage"):
            with InfoStorage.open_current(str(self._db_path)) as storage:
                yield storage
            return
        if self._storage is None:
            self._storage = InfoStorage.open_current(str(self._db_path))
        yield self._storage

    def end_headers(self) -> None:
        parsed = urlparse(self.path)
        if self.command != "POST" and (
            self.headers.get("Content-Length") not in (None, "0")
            or self.headers.get("Transfer-Encoding") is not None
        ):
            self.close_connection = True
        if parsed.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "style-src-attr 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()

    def do_GET(self) -> None:
        if not self._request_host_is_loopback():
            self._write_json(
                {"error": "loopback Host required"},
                status=HTTPStatus.MISDIRECTED_REQUEST,
            )
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/entries":
            self._handle_entries(parsed.query)
            return
        if parsed.path == "/api/ins/status":
            self._handle_ins_status()
            return
        if parsed.path == "/api/tag-tree":
            self._handle_tag_tree(parsed.query)
            return
        if parsed.path == "/api/health":
            self._write_json({"ok": True})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        if self.command == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._close_after_body = self.close_connection
        self.close_connection = True
        if not self._request_host_is_loopback():
            self._write_json(
                {"error": "loopback Host required"},
                status=HTTPStatus.MISDIRECTED_REQUEST,
            )
            return
        if not self._request_is_same_origin():
            self._write_json(
                {"error": "same-origin request required"},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        if self.headers.get_content_type() != "application/json":
            self._write_json(
                {"error": "Content-Type must be application/json"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/ins/run":
            self._handle_ins_run()
            return
        if parsed.path == "/api/remove-entry":
            self._handle_remove_entry()
            return
        if parsed.path == "/api/restore-entry":
            self._handle_restore_entry()
            return
        if parsed.path == "/api/favored":
            self._handle_favored()
            return
        if parsed.path == "/api/noticed":
            self._handle_noticed()
            return
        self._write_json(
            {"error": "not found"},
            status=HTTPStatus.NOT_FOUND,
        )

    def _request_is_same_origin(self) -> bool:
        host = _http_authority(self.headers.get("Host", ""))
        try:
            origin = urlparse(self.headers.get("Origin", ""))
        except ValueError:
            return False
        origin_authority = _http_authority(origin.netloc)
        return (
            host is not None
            and origin.scheme == "http"
            and origin_authority == host
            and not origin.path
            and not origin.params
            and not origin.query
            and not origin.fragment
        )

    def _request_host_is_loopback(self) -> bool:
        authority = _http_authority(self.headers.get("Host", ""))
        return authority is not None and _is_loopback_host(authority[0])

    def _handle_entries(self, raw_query: str) -> None:
        try:
            query = parse_qs(raw_query, max_num_fields=MAX_QUERY_FIELDS)
            limit = _parse_int(query.get("limit", ["30"])[
                               0], 30, min_value=1, max_value=200)
            cursor = _decode_cursor(query.get("cursor", [""])[0])
            options = _entry_query_options(query)
        except ValueError as exc:
            self._write_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        include_total = _parse_flag(query.get("include_total", ["1"])[0])

        try:
            with self._current_storage() as storage:
                result = storage.query_entries(
                    **options,
                    limit=limit,
                    cursor=cursor,
                    include_total=cursor is None and include_total,
                )
        except Exception as exc:
            self._write_json(
                {"error": f"failed to read database: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self._write_json(
            {
                "items": result["items"],
                "total": result["total"],
                "limit": limit,
                "has_more": result["has_more"],
                "next_cursor": _encode_cursor(result["next_position"]),
            }
        )

    def _handle_tag_tree(self, raw_query: str) -> None:
        try:
            query = parse_qs(raw_query, max_num_fields=MAX_QUERY_FIELDS)
            options = _entry_query_options(query)
        except ValueError as exc:
            self._write_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            configured_groups = _load_configured_sources(self._conf_path)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to read configuration: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        configured_tags = {
            str(child.get("selector_value", ""))
            for group in configured_groups
            for child in group.get("children", [])
        }
        group_selectors = [
            {
                str(child.get("selector_value", ""))
                for child in group.get("children", [])
            }
            for group in configured_groups
        ]
        try:
            with self._current_storage() as storage:
                facets = storage.query_facets(
                    configured_tags=configured_tags,
                    groups=group_selectors,
                    **options,
                )
        except Exception as exc:
            self._write_json(
                {"error": f"failed to read database: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        groups = []
        total_sources = 0
        for group_index, group in enumerate(configured_groups):
            srce_ty = str(group.get("name", ""))
            children = []
            for item in group.get("children", []):
                selector_value = str(item.get("selector_value", ""))
                children.append(
                    {
                        "name": str(item.get("name", "")),
                        "selector_value": selector_value,
                        "count": int(facets["tag_counts"].get(selector_value, 0)),
                    }
                )

            total_sources += len(children)
            groups.append(
                {
                    "name": srce_ty,
                    "count": facets["group_counts"][group_index],
                    "children": children,
                }
            )

        self._write_json(
            {
                "root": {
                    "name": "配置源",
                    "group_count": len(groups),
                    "source_count": total_sources,
                    "count": facets["total"],
                },
                "groups": groups,
            }
        )

    def _read_json_body(self) -> dict | None:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            return None
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            return None
        self.close_connection = getattr(self, "_close_after_body", True)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _source_key_from_payload(payload: dict) -> tuple[str, str]:
        raw_srce_ty = payload.get("srce_ty")
        raw_srce_id = payload.get("srce_id")
        if not isinstance(raw_srce_ty, str) or not isinstance(raw_srce_id, str):
            return "", ""
        srce_ty = raw_srce_ty.strip()
        srce_id = raw_srce_id.strip()
        if (
            len(srce_ty) > MAX_SOURCE_TYPE_LENGTH
            or len(srce_id) > MAX_SOURCE_ID_LENGTH
        ):
            return "", ""
        return srce_ty, srce_id

    @staticmethod
    def _binary_value_from_payload(payload: dict, field: str) -> int | None:
        raw_value = payload.get(field)
        if isinstance(raw_value, bool):
            return None
        if isinstance(raw_value, int) and raw_value in (0, 1):
            return raw_value
        if isinstance(raw_value, str) and raw_value in ("0", "1"):
            return int(raw_value)
        return None

    @staticmethod
    def _revision_from_payload(payload: dict) -> int | None:
        revision = payload.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return None
        return revision if 0 <= revision <= SQLITE_INT_MAX else None

    def _handle_favored(self) -> None:
        self._handle_flag("favored")

    def _handle_noticed(self) -> None:
        self._handle_flag("noticed")

    def _handle_flag(self, field: str) -> None:
        update = {
            "favored": InfoStorage.favor_entry_if_current,
            "noticed": InfoStorage.notice_entry_if_current,
        }[field]
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        value = self._binary_value_from_payload(payload, field)
        expected = self._binary_value_from_payload(payload, f"expected_{field}")
        revision = self._revision_from_payload(payload)
        if value is None or expected is None or revision is None or value == expected:
            self._write_json(
                {"error": (
                    f"srce_ty, srce_id, {field}(0/1) and "
                    f"expected_{field}(0/1), expected_revision are required; "
                    f"{field} values must differ"
                )},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        srce_ty, srce_id = self._source_key_from_payload(payload)
        if not srce_ty or not srce_id:
            self._write_json(
                {"error": "srce_ty and srce_id are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            with self._current_storage() as storage:
                changed = update(storage, srce_ty, srce_id, expected, revision, value)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to update {field}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        if not changed:
            self._write_json(
                {"error": f"entry {field} state changed"},
                status=HTTPStatus.CONFLICT,
            )
            return
        self._write_json({
            "ok": True,
            "updated": int(changed),
            "srce_ty": srce_ty,
            "srce_id": srce_id,
            field: value,
            "state_rev": revision + 1,
        })

    def _handle_remove_entry(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        srce_ty, srce_id = self._source_key_from_payload(payload)
        if not srce_ty or not srce_id:
            self._write_json(
                {"error": "srce_ty and srce_id are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with REMOVE_UNDO_LOCK:
                with self._current_storage() as storage:
                    entry = storage.pop_entry(
                        srce_ty,
                        srce_id,
                    )
                if entry is not None:
                    undo_token = secrets.token_urlsafe(24)
                    REMOVE_UNDO[undo_token] = entry
                    if len(REMOVE_UNDO) > MAX_REMOVE_UNDOS:
                        REMOVE_UNDO.pop(next(iter(REMOVE_UNDO)))
        except Exception as exc:
            self._write_json(
                {"error": f"failed to remove entry: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if entry is None:
            self._write_json(
                {"error": "entry not found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        self._write_json(
            {
                "ok": True,
                "removed": 1,
                "srce_ty": srce_ty,
                "srce_id": srce_id,
                "undo_token": undo_token,
            }
        )

    def _handle_restore_entry(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        token = payload.get("undo_token")
        if not isinstance(token, str) or not token:
            self._write_json(
                {"error": "a valid undo_token is required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with REMOVE_UNDO_LOCK:
                entry = REMOVE_UNDO.get(token)
                if isinstance(entry, dict):
                    with self._current_storage() as storage:
                        restored = storage.restore_entry(entry)
                    REMOVE_UNDO.pop(token, None)
                else:
                    entry = None
        except Exception as exc:
            self._write_json(
                {"error": f"failed to restore entry: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if entry is None:
            self._write_json(
                {"error": "undo operation is no longer available"},
                status=HTTPStatus.CONFLICT,
            )
            return

        self._write_json(
            {
                "ok": True,
                "restored": int(restored),
                "already_present": restored == 0,
                "srce_ty": entry["srce_ty"],
                "srce_id": entry["srce_id"],
            }
        )

    def _handle_ins_status(self) -> None:
        self._write_json({"ok": True, "job": _ins_snapshot()})

    def _handle_ins_run(self) -> None:
        if self._read_json_body() is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        with INS_LOCK:
            if INS_JOB["state"] == "running":
                running_snapshot = _ins_snapshot_unlocked()
                self._write_json(
                    {"ok": False, "error": "ins is already running",
                        "job": running_snapshot},
                    status=HTTPStatus.CONFLICT,
                )
                return

            INS_JOB["state"] = "running"
            INS_JOB["progress"] = 1
            INS_JOB["message"] = "启动中"
            INS_JOB["started_at"] = _utcnow_iso()
            INS_JOB["ended_at"] = None

        try:
            worker = threading.Thread(
                target=_run_ins_job,
                args=(self._db_path, self._conf_path),
                daemon=True,
            )
            worker.start()
        except Exception as exc:
            _ins_update(
                state="failed",
                message=f"启动失败: {exc}",
                ended_at=_utcnow_iso(),
            )
            self._write_json(
                {"ok": False, "error": "failed to start ingestion"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._write_json({"ok": True, "job": _ins_snapshot()})

    def _write_json(self, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve the local web UI and database API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--conf", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        parser.error("--host must be a loopback address or localhost")

    try:
        db_path = _normalize_db_path(args.db_path)
    except ValueError as exc:
        parser.error(str(exc))
    conf_path = Path(args.conf).expanduser().resolve()
    with InfoStorage(db_path):
        pass
    handler = partial(InfoHandler, db_path=db_path, conf_path=conf_path)
    bind_host = args.host.strip().strip("[]")
    if bind_host.casefold() == "localhost":
        bind_host = "localhost"
    server_class = (
        ThreadingHTTPServerV6
        if ipaddress.ip_address(bind_host).version == 6
        else InfoHTTPServer
    ) if bind_host != "localhost" else InfoHTTPServer
    display_host = f"[{bind_host}]" if server_class is ThreadingHTTPServerV6 else bind_host
    with server_class((bind_host, args.port), handler) as server:
        print(
            f"Serving on http://{display_host}:{args.port} "
            f"(db: {db_path}, conf: {conf_path})"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
