from .storage import InfoStorage
from .paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, WEB_DIR
from .cli import _cmd_ins

import argparse
import base64
import json
import secrets
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
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
REMOVE_UNDO = {"token": None, "entry": None}
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CURSOR_BYTES = 16_384
SQLITE_INT_MIN = -(2 ** 63)
SQLITE_INT_MAX = 2 ** 63 - 1


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
        for part in value.split(","):
            selector = part.strip()
            if not selector:
                continue
            if selector.startswith("tag:"):
                tag = selector[4:].strip()
                if tag:
                    tag_values.add(tag)
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
        or len(value[1]) > 512
        or not isinstance(value[2], str)
        or len(value[2]) > 4096
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
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
        "query_text": query.get("q", [""])[0].strip(),
    }


def _extract_arxiv_tag(url: str) -> str | None:
    if not url:
        return None
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    tag = path.split("/")[-1].strip()
    return tag or None


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


def _clear_removed_entry() -> None:
    with REMOVE_UNDO_LOCK:
        REMOVE_UNDO["token"] = None
        REMOVE_UNDO["entry"] = None


def _run_ins_job(db_path: str | Path, conf_path: Path) -> None:
    try:
        _ins_update(progress=10, message="正在拉取")
        _cmd_ins(
            argparse.Namespace(db_path=db_path, conf=conf_path)
        )
        _ins_update(
            state="succeeded",
            progress=100,
            message="拉取完成",
            ended_at=_utcnow_iso(),
        )
    except Exception as exc:
        _ins_update(
            state="failed",
            progress=max(int(_ins_snapshot()["progress"]), 1),
            message=f"拉取失败: {exc}",
            ended_at=_utcnow_iso(),
        )


def _load_configured_sources(conf_path: Path) -> list[dict]:
    if not conf_path.exists():
        return []
    with conf_path.open("rb") as f:
        conf = tomllib.load(f)

    raw_sources = conf.get("arXiv", [])
    if not isinstance(raw_sources, list):
        return []
    children = []
    seen = set()
    for idx, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip() or f"arXiv-{idx + 1}"
        url = str(item.get("url", "")).strip()
        tag = _extract_arxiv_tag(url)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        children.append({"name": name, "url": url, "selector_value": tag})
    return [{"name": "arXiv", "children": children}]


def _normalize_db_path(raw_path: str) -> str | Path:
    if raw_path.startswith("file:"):
        return raw_path
    return Path(raw_path).expanduser().resolve()


class InfoHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args,
        db_path: str | Path,
        conf_path: Path,
        **kwargs,
    ) -> None:
        self._db_path = db_path
        self._conf_path = conf_path
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def do_GET(self) -> None:
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
        super().do_GET()

    def do_POST(self) -> None:
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

    def _handle_entries(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        limit = _parse_int(query.get("limit", ["30"])[
                           0], 30, min_value=1, max_value=200)
        try:
            cursor = _decode_cursor(query.get("cursor", [""])[0])
        except ValueError as exc:
            self._write_json(
                {"error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        options = _entry_query_options(query)
        include_total = _parse_flag(query.get("include_total", ["1"])[0])

        try:
            with InfoStorage(str(self._db_path)) as storage:
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
        query = parse_qs(raw_query)
        options = _entry_query_options(query)

        try:
            configured_groups = _load_configured_sources(self._conf_path)
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
            with InfoStorage(str(self._db_path)) as storage:
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
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError:
            return None
        if length <= 0 or length > MAX_JSON_BODY_BYTES:
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _source_key_from_payload(payload: dict) -> tuple[str, str]:
        srce_ty = str(payload.get("srce_ty", "")).strip()
        srce_id = str(payload.get("srce_id", "")).strip()
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
        return revision if revision >= 0 else None

    def _handle_entry_mutation(
        self,
        payload: dict,
        *,
        action_error: str,
        action: Callable[[InfoStorage, str, str], int],
        success_count_key: str,
        success_extra: dict[str, object] | None = None,
        no_change_status: HTTPStatus = HTTPStatus.NOT_FOUND,
        no_change_error: str = "entry not found",
    ) -> None:
        srce_ty, srce_id = self._source_key_from_payload(payload)
        if not srce_ty or not srce_id:
            self._write_json(
                {"error": "srce_ty and srce_id are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with InfoStorage(str(self._db_path)) as storage:
                changed = action(storage, srce_ty, srce_id)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to {action_error}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if not changed:
            self._write_json(
                {"error": no_change_error},
                status=no_change_status,
            )
            return

        _clear_removed_entry()

        response = {
            "ok": True,
            success_count_key: int(changed),
            "srce_ty": srce_ty,
            "srce_id": srce_id,
        }
        if success_extra:
            response.update(success_extra)
        self._write_json(response)

    def _handle_favored(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        favored = self._binary_value_from_payload(payload, "favored")
        expected = self._binary_value_from_payload(payload, "expected_favored")
        expected_revision = self._revision_from_payload(payload)
        if (
            favored is None
            or expected is None
            or expected_revision is None
            or favored == expected
        ):
            self._write_json(
                {
                    "error": (
                        "srce_ty, srce_id, favored(0/1) and "
                        "expected_favored(0/1), expected_revision are required; "
                        "favored values must differ"
                    )
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        self._handle_entry_mutation(
            payload,
            action_error="update favored",
            action=lambda storage, srce_ty, srce_id: storage.favor_entry_if_current(
                srce_ty, srce_id, expected, expected_revision, favored
            ),
            success_count_key="updated",
            success_extra={
                "favored": favored,
                "state_rev": expected_revision + 1,
            },
            no_change_status=HTTPStatus.CONFLICT,
            no_change_error="entry favored state changed",
        )

    def _handle_noticed(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        noticed = self._binary_value_from_payload(payload, "noticed")
        expected = self._binary_value_from_payload(payload, "expected_noticed")
        expected_revision = self._revision_from_payload(payload)
        if (
            noticed is None
            or expected is None
            or expected_revision is None
            or noticed == expected
        ):
            self._write_json(
                {
                    "error": (
                        "srce_ty, srce_id, noticed(0/1) and "
                        "expected_noticed(0/1), expected_revision are required; "
                        "noticed values must differ"
                    )
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        self._handle_entry_mutation(
            payload,
            action_error="update noticed",
            action=lambda storage, srce_ty, srce_id: storage.notice_entry_if_current(
                srce_ty, srce_id, expected, expected_revision, noticed
            ),
            success_count_key="updated",
            success_extra={
                "noticed": noticed,
                "state_rev": expected_revision + 1,
            },
            no_change_status=HTTPStatus.CONFLICT,
            no_change_error="entry noticed state changed",
        )

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
                with InfoStorage(str(self._db_path)) as storage:
                    entry = storage.pop_entry(
                        srce_ty,
                        srce_id,
                    )
                if entry is not None:
                    undo_token = secrets.token_urlsafe(24)
                    REMOVE_UNDO["token"] = undo_token
                    REMOVE_UNDO["entry"] = entry
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
                "entry": entry,
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
                entry = REMOVE_UNDO["entry"]
                if REMOVE_UNDO["token"] != token or not isinstance(entry, dict):
                    entry = None
                else:
                    with InfoStorage(str(self._db_path)) as storage:
                        restored = storage.restore_entry(entry)
                    REMOVE_UNDO["token"] = None
                    REMOVE_UNDO["entry"] = None
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

        worker = threading.Thread(
            target=_run_ins_job,
            args=(self._db_path, self._conf_path),
            daemon=True,
        )
        worker.start()
        self._write_json({"ok": True, "job": _ins_snapshot()})

    def _write_json(self, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve the local web UI and database API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--conf", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    db_path = _normalize_db_path(args.db_path)
    conf_path = Path(args.conf).expanduser().resolve()
    with InfoStorage(db_path):
        pass
    handler = partial(InfoHandler, db_path=db_path, conf_path=conf_path)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(
            f"Serving on http://{args.host}:{args.port} "
            f"(db: {db_path}, conf: {conf_path})"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
