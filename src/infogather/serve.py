from .storage import InfoStorage

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "entries.db"
DEFAULT_CONF_PATH = PROJECT_ROOT / "conf" / "config.toml"
DEFAULT_INF_BIN = PROJECT_ROOT / ".venv" / "bin" / "inf"

FETCH_PROGRESS_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)-")

INS_LOCK = threading.Lock()
INS_JOB = {
    "state": "idle",  # idle | running | succeeded | failed
    "progress": 0,
    "message": "就绪",
    "started_at": None,
    "ended_at": None,
    "returncode": None,
    "logs": [],
}


def _parse_int(raw: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, min_value), max_value)


def _parse_flag(raw: str) -> bool:
    return raw.strip() == "1"


def _parse_tags(values: list[str]) -> list[str]:
    tags = []
    seen = set()
    for value in values:
        for part in value.split(","):
            tag = part.strip()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            tags.append(tag)
    return tags


def _parse_selectors(values: list[str]) -> tuple[set[str], set[str]]:
    tag_values: set[str] = set()
    source_types: set[str] = set()
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
            if selector.startswith("source_type:"):
                srce_ty = selector[len("source_type:"):].strip()
                if srce_ty:
                    source_types.add(srce_ty)
                continue
            # Backward compatibility: treat unknown selector as a tag.
            tag_values.add(selector)
    return tag_values, source_types


def _parse_updated(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _updated_timestamp(value: str) -> float:
    dt = _parse_updated(value)
    if dt is None:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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


def _ins_snapshot() -> dict:
    with INS_LOCK:
        return {
            "state": INS_JOB["state"],
            "progress": INS_JOB["progress"],
            "message": INS_JOB["message"],
            "started_at": INS_JOB["started_at"],
            "ended_at": INS_JOB["ended_at"],
            "returncode": INS_JOB["returncode"],
            "logs": list(INS_JOB["logs"]),
        }


def _ins_update(**kwargs: object) -> None:
    with INS_LOCK:
        INS_JOB.update(kwargs)


def _ins_append_log(line: str) -> None:
    with INS_LOCK:
        INS_JOB["logs"].append(line)
        if len(INS_JOB["logs"]) > 120:
            INS_JOB["logs"] = INS_JOB["logs"][-120:]


def _ins_progress_from_line(line: str, current: int) -> tuple[int, str]:
    if "Fetching feeds from" in line:
        return max(current, 8), "开始抓取源"

    match = FETCH_PROGRESS_RE.search(line)
    if match:
        idx = int(match.group(1))
        total = max(int(match.group(2)), 1)
        progress = 10 + int((idx / total) * 45)
        return max(current, progress), f"抓取源 {idx}/{total}"

    if "Normalizing feeds..." in line:
        return max(current, 62), "正在归一化"

    if "Insert result:" in line:
        return max(current, 95), "正在写入数据库"

    if line.strip():
        return min(max(current, 8) + 1, 90), line.strip()

    return current, "处理中"


def _run_ins_job() -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if DEFAULT_INF_BIN.exists():
        cmd = [str(DEFAULT_INF_BIN), "ins"]
    else:
        cmd = [sys.executable, "-m", "infogather.main", "ins"]
        env["PYTHONPATH"] = (
            f"{SRC_DIR}:{env.get('PYTHONPATH', '')}"
            if env.get("PYTHONPATH")
            else str(SRC_DIR)
        )
    _ins_append_log(f"$ {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as exc:
        _ins_update(
            state="failed",
            progress=0,
            message=f"启动失败: {exc}",
            ended_at=_utcnow_iso(),
            returncode=-1,
        )
        _ins_append_log(f"[error] {exc}")
        return

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        _ins_append_log(line)
        snap = _ins_snapshot()
        progress, message = _ins_progress_from_line(
            line, int(snap["progress"]))
        _ins_update(progress=progress, message=message)

    returncode = proc.wait()
    if returncode == 0:
        _ins_update(
            state="succeeded",
            progress=100,
            message="拉取完成",
            ended_at=_utcnow_iso(),
            returncode=0,
        )
    else:
        _ins_update(
            state="failed",
            progress=max(int(_ins_snapshot()["progress"]), 1),
            message=f"拉取失败 (exit={returncode})",
            ended_at=_utcnow_iso(),
            returncode=returncode,
        )


def _load_configured_sources(conf_path: Path) -> list[dict]:
    if not conf_path.exists():
        return []
    try:
        with conf_path.open("rb") as f:
            conf = tomllib.load(f)
    except Exception:
        return []

    groups: list[dict] = []
    for srce_ty, raw_sources in conf.items():
        if not isinstance(raw_sources, list):
            continue

        children = []
        seen = set()
        for idx, item in enumerate(raw_sources):
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip() or f"{srce_ty}-{idx + 1}"
            url = str(item.get("url", "")).strip()

            if srce_ty == "arXiv":
                tag = _extract_arxiv_tag(url)
                if not tag or tag in seen:
                    continue
                seen.add(tag)
                children.append(
                    {
                        "name": name,
                        "url": url,
                        "selector_type": "tag",
                        "selector_value": tag,
                    }
                )
                continue

            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            children.append(
                {
                    "name": name,
                    "url": url,
                    "selector_type": "source_type",
                    "selector_value": str(srce_ty),
                }
            )

        groups.append({"name": str(srce_ty), "children": children})
    return groups


class InfoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, db_path: Path, **kwargs) -> None:
        self._db_path = db_path
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header(
            "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
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

    def _build_entry_filter(self, query: dict[str, list[str]]):
        favored = _parse_flag(query.get("favored", [""])[0])
        unnoticed = _parse_flag(query.get("unnoticed", [""])[0])
        updated_within_day = _parse_flag(
            query.get("updated_within_day", [""])[0])
        updated_within_week = _parse_flag(
            query.get("updated_within_week", [""])[0])
        version_is_1 = _parse_flag(query.get("version_is_1", [""])[0])
        version_is_not_1 = _parse_flag(
            query.get("version_is_not_1", [""])[0])
        selected_tags, selected_source_types = _parse_selectors(
            query.get("selectors", []))
        # Backward compatibility for older clients.
        selected_tags.update(_parse_tags(query.get("tags", [])))
        q = query.get("q", [""])[0].strip().lower()
        now = datetime.now(timezone.utc)

        def entry_filter(entry: dict) -> bool:
            if favored and int(entry.get("favored", 0)) != 1:
                return False
            if unnoticed and int(entry.get("noticed", 0)) != 0:
                return False
            if version_is_1 and int(entry.get("version", 0)) != 1:
                return False
            if version_is_not_1 and int(entry.get("version", 0)) == 1:
                return False

            updated = _parse_updated(str(entry.get("updated", "")))
            if updated_within_day:
                if updated is None:
                    return False
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if now - updated.astimezone(timezone.utc) > timedelta(days=1):
                    return False
            if updated_within_week:
                if updated is None:
                    return False
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if now - updated.astimezone(timezone.utc) > timedelta(days=7):
                    return False

            content = entry.get("content", {})
            if selected_tags or selected_source_types:
                entry_tags = set(content.get("tags", []) or [])
                by_tag = bool(entry_tags.intersection(selected_tags))
                by_source_type = str(
                    entry.get("srce_ty", "")) in selected_source_types
                if not (by_tag or by_source_type):
                    return False

            if q:
                haystack = " ".join(
                    [
                        str(entry.get("srce_id", "")),
                        str(content.get("titl", "")),
                        str(content.get("auth", "")),
                        str(content.get("abst", "")),
                        " ".join(map(str, content.get("tags", []) or [])),
                    ]
                ).lower()
                return q in haystack
            return True

        return entry_filter

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ins/run":
            self._handle_ins_run()
            return
        if parsed.path == "/api/remove-entry":
            self._handle_remove_entry()
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
        offset = _parse_int(query.get("offset", ["0"])[
                            0], 0, min_value=0, max_value=1_000_000)
        entry_filter = self._build_entry_filter(query)

        try:
            with InfoStorage(str(self._db_path)) as storage:
                all_items = storage.export_entries_json(
                    entry_filter=entry_filter)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to read database: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        all_items.sort(
            key=lambda item: (
                -_updated_timestamp(str(item.get("updated", ""))),
                str(item.get("srce_id", "")),
            )
        )
        total = len(all_items)
        items = all_items[offset: offset + limit]
        self._write_json(
            {"items": items, "total": total, "limit": limit, "offset": offset}
        )

    def _handle_tag_tree(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        entry_filter = self._build_entry_filter(query)
        configured_groups = _load_configured_sources(DEFAULT_CONF_PATH)

        try:
            with InfoStorage(str(self._db_path)) as storage:
                all_items = storage.export_entries_json(
                    entry_filter=entry_filter)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to read database: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        tag_counter = Counter()
        source_type_counter = Counter()
        for entry in all_items:
            source_type_counter[str(entry.get("srce_ty", ""))] += 1
            tags = entry.get("content", {}).get("tags", []) or []
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                clean = tag.strip()
                if clean:
                    tag_counter[clean] += 1

        groups = []
        total_sources = 0
        for group in configured_groups:
            srce_ty = str(group.get("name", ""))
            children = []
            group_count = 0
            for item in group.get("children", []):
                selector_type = str(item.get("selector_type", ""))
                selector_value = str(item.get("selector_value", ""))
                count = 0
                if selector_type == "tag":
                    count = int(tag_counter.get(selector_value, 0))
                elif selector_type == "source_type":
                    count = int(source_type_counter.get(selector_value, 0))

                children.append(
                    {
                        "name": str(item.get("name", "")),
                        "url": str(item.get("url", "")),
                        "selector_type": selector_type,
                        "selector_value": selector_value,
                        "count": count,
                    }
                )

            group_tags = {
                str(child.get("selector_value", ""))
                for child in children
                if child.get("selector_type") == "tag"
            }
            group_source_types = {
                str(child.get("selector_value", ""))
                for child in children
                if child.get("selector_type") == "source_type"
            }
            matched_entry_keys = set()
            for entry in all_items:
                entry_key = f"{entry.get('srce_ty', '')}:{entry.get('srce_id', '')}"
                entry_source_type = str(entry.get("srce_ty", ""))
                entry_tags = set(
                    entry.get("content", {}).get("tags", []) or [])
                if entry_source_type in group_source_types or entry_tags.intersection(group_tags):
                    matched_entry_keys.add(entry_key)
            group_count = len(matched_entry_keys)

            total_sources += len(children)
            groups.append(
                {"name": srce_ty, "count": group_count, "children": children})

        self._write_json(
            {
                "root": {
                    "name": "配置源",
                    "group_count": len(groups),
                    "source_count": total_sources,
                    "count": len(all_items),
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
        if length <= 0:
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _handle_favored(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        srce_ty = str(payload.get("srce_ty", "")).strip()
        srce_id = str(payload.get("srce_id", "")).strip()
        favored_raw = payload.get("favored")
        try:
            favored = int(favored_raw)
        except (TypeError, ValueError):
            favored = -1

        if not srce_ty or not srce_id or favored not in (0, 1):
            self._write_json(
                {"error": "srce_ty, srce_id and favored(0/1) are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with InfoStorage(str(self._db_path)) as storage:
                updated = storage.favor_entry(srce_ty, srce_id, favored)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to update favored: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if not updated:
            self._write_json(
                {"error": "entry not found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        self._write_json(
            {"ok": True, "updated": int(
                updated), "srce_ty": srce_ty, "srce_id": srce_id, "favored": favored}
        )

    def _handle_noticed(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        srce_ty = str(payload.get("srce_ty", "")).strip()
        srce_id = str(payload.get("srce_id", "")).strip()
        noticed_raw = payload.get("noticed")
        try:
            noticed = int(noticed_raw)
        except (TypeError, ValueError):
            noticed = -1

        if not srce_ty or not srce_id or noticed not in (0, 1):
            self._write_json(
                {"error": "srce_ty, srce_id and noticed(0/1) are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with InfoStorage(str(self._db_path)) as storage:
                updated = storage.notice_entry(srce_ty, srce_id, noticed)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to update noticed: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if not updated:
            self._write_json(
                {"error": "entry not found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        self._write_json(
            {"ok": True, "updated": int(
                updated), "srce_ty": srce_ty, "srce_id": srce_id, "noticed": noticed}
        )

    def _handle_remove_entry(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._write_json(
                {"error": "invalid JSON body"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        srce_ty = str(payload.get("srce_ty", "")).strip()
        srce_id = str(payload.get("srce_id", "")).strip()
        if not srce_ty or not srce_id:
            self._write_json(
                {"error": "srce_ty and srce_id are required"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            with InfoStorage(str(self._db_path)) as storage:
                removed = storage.remove_entry(srce_ty, srce_id)
        except Exception as exc:
            self._write_json(
                {"error": f"failed to remove entry: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if not removed:
            self._write_json(
                {"error": "entry not found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        self._write_json(
            {"ok": True, "removed": int(
                removed), "srce_ty": srce_ty, "srce_id": srce_id}
        )

    def _handle_ins_status(self) -> None:
        self._write_json({"ok": True, "job": _ins_snapshot()})

    def _handle_ins_run(self) -> None:
        with INS_LOCK:
            if INS_JOB["state"] == "running":
                running_snapshot = {
                    "state": INS_JOB["state"],
                    "progress": INS_JOB["progress"],
                    "message": INS_JOB["message"],
                    "started_at": INS_JOB["started_at"],
                    "ended_at": INS_JOB["ended_at"],
                    "returncode": INS_JOB["returncode"],
                    "logs": list(INS_JOB["logs"]),
                }
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
            INS_JOB["returncode"] = None
            INS_JOB["logs"] = []

        worker = threading.Thread(target=_run_ins_job, daemon=True)
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
        description="Serve local web UI and read-only DB API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    handler = partial(InfoHandler, db_path=db_path)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving on http://{args.host}:{args.port} (db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
