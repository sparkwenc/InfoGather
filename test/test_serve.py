import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from http import HTTPStatus

from infogather.serve import (
    InfoHandler,
    InfoStorage,
    ThreadingHTTPServerV6,
    _decode_cursor,
    _encode_cursor,
    _is_loopback_host,
    _normalize_db_path,
    _run_ins_job,
)
from infogather.ingestion import IngestionResult
import infogather.serve as serve


def _seed_entry(db_path: Path) -> None:
    with InfoStorage(str(db_path)) as storage:
        with redirect_stdout(io.StringIO()):
            storage.insert_entries(
                [
                    {
                        "srce_ty": "arXiv",
                        "srce_id": "2601.00001",
                        "version": 1,
                        "favored": 0,
                        "noticed": 0,
                        "updated": "2026-03-01T00:00:00+00:00",
                        "content": {
                            "link": "https://arxiv.org/abs/2601.00001",
                            "titl": "Example title",
                            "auth": "Example author",
                            "abst": "Example abstract",
                            "tags": ["math.AG"],
                        },
                    },
                ]
            )


@contextmanager
def _running_server(db_path: Path):
    handler = partial(
        InfoHandler,
        db_path=db_path,
        conf_path=db_path.parent / "config.toml",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever)
    with mock.patch.object(InfoHandler, "log_message"):
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def _request(server, method: str, path: str, *, body=None, headers=None):
    connection = HTTPConnection(*server.server_address)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, json.loads(data)


class HandlerHarness(InfoHandler):
    def __init__(self, db_path: Path, payload: dict | None) -> None:
        self._db_path = db_path
        self._conf_path = db_path.parent / "config.toml"
        self._payload = payload
        self.status = HTTPStatus.OK
        self.response: dict | None = None

    def _read_json_body(self) -> dict | None:
        return self._payload

    def _write_json(self, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.status = status
        self.response = payload


class ServeInsTests(unittest.TestCase):
    def setUp(self) -> None:
        with serve.INS_LOCK:
            serve.INS_JOB["state"] = "running"
            serve.INS_JOB["progress"] = 1
            serve.INS_JOB["message"] = "test"
            serve.INS_JOB["started_at"] = None
            serve.INS_JOB["ended_at"] = None

    def test_run_ins_job_passes_database_and_config_paths(self) -> None:
        result = IngestionResult(2, 0, 1, 3, 2)
        def ingest(db_path, conf_path, *, progress):
            progress(50, "Annals of Mathematics: 拉取 50 条")
            return result

        with mock.patch.object(serve, "run_ingestion", side_effect=ingest) as ingestion:
            db_path = Path("/tmp/custom-entries.db")
            conf_path = Path("/tmp/custom-config.toml")
            _run_ins_job(db_path, conf_path)

        self.assertEqual(ingestion.call_args.args, (db_path, conf_path))
        self.assertEqual(serve.INS_JOB["state"], "succeeded")
        self.assertEqual(
            serve.INS_JOB["message"],
            "写入 2/3 条",
        )

    def test_ins_run_can_return_an_already_completed_job(self) -> None:
        class ImmediateThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        result = IngestionResult(0, 0, 0, 0, 0)
        with tempfile.TemporaryDirectory() as td:
            with serve.INS_LOCK:
                serve.INS_JOB["state"] = "idle"
            harness = HandlerHarness(Path(td) / "entries.db", {})
            with (
                mock.patch.object(serve.threading, "Thread", ImmediateThread),
                mock.patch.object(
                    serve,
                    "run_ingestion",
                    side_effect=lambda *_args, **_kwargs: result,
                ),
            ):
                harness._handle_ins_run()

        self.assertEqual(harness.status, HTTPStatus.OK)
        self.assertEqual(harness.response["job"]["state"], "succeeded")

    def test_web_assets_are_packaged_with_server(self) -> None:
        self.assertTrue((serve.WEB_DIR / "index.html").is_file())
        index = (serve.WEB_DIR / "index.html").read_text()
        self.assertNotIn("cdn.jsdelivr.net", index)
        self.assertTrue(
            (serve.WEB_DIR / "vendor" / "katex" / "katex.min.js").is_file()
        )

    def test_server_accepts_only_loopback_bindings(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(_is_loopback_host(host))
        for host in ("0.0.0.0", "::", "192.168.1.2", "example.com"):
            self.assertFalse(_is_loopback_host(host))

    def test_server_keeps_http_1_1_connections_alive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            with _running_server(db_path) as server:
                connection = HTTPConnection(*server.server_address)
                try:
                    with mock.patch.object(
                        InfoStorage,
                        "open_current",
                        wraps=InfoStorage.open_current,
                    ) as open_current:
                        connection.request("GET", "/api/entries")
                        first = connection.getresponse()
                        self.assertEqual(first.version, 11)
                        first.read()
                        socket = connection.sock

                        connection.request("GET", "/api/entries")
                        second = connection.getresponse()
                        second.read()
                        self.assertIs(connection.sock, socket)
                        self.assertEqual(open_current.call_count, 1)
                finally:
                    connection.close()

    def test_ipv6_loopback_server_can_bind(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            handler = partial(
                InfoHandler,
                db_path=root / "entries.db",
                conf_path=root / "config.toml",
            )
            try:
                server = ThreadingHTTPServerV6(("::1", 0), handler)
            except OSError as exc:
                self.skipTest(f"IPv6 loopback is unavailable: {exc}")
            server.server_close()

    def test_http_boundary_enforces_same_origin_json_and_security_headers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            payload = json.dumps({
                "srce_ty": "arXiv",
                "srce_id": "2601.00001",
                "favored": 1,
                "expected_favored": 0,
                "expected_revision": 0,
            })
            with _running_server(db_path) as server:
                origin = f"http://127.0.0.1:{server.server_port}"
                health, health_body = _request(server, "GET", "/api/health")
                rebound_get, _ = _request(
                    server,
                    "GET",
                    "/api/health",
                    headers={"Host": "rebind.example"},
                )
                rebound_post, _ = _request(
                    server,
                    "POST",
                    "/api/favored",
                    body=payload,
                    headers={
                        "Host": "rebind.example",
                        "Origin": "http://rebind.example",
                        "Content-Type": "application/json",
                    },
                )
                missing_origin, _ = _request(
                    server,
                    "POST",
                    "/api/favored",
                    body=payload,
                    headers={"Content-Type": "application/json"},
                )
                wrong_type, _ = _request(
                    server,
                    "POST",
                    "/api/favored",
                    body=payload,
                    headers={"Content-Type": "text/plain", "Origin": origin},
                )
                accepted, accepted_body = _request(
                    server,
                    "POST",
                    "/api/favored",
                    body=payload,
                    headers={"Content-Type": "application/json", "Origin": origin},
                )

        self.assertEqual(health.status, HTTPStatus.OK)
        self.assertTrue(health_body["ok"])
        self.assertIn("default-src 'self'", health.getheader(
            "Content-Security-Policy"
        ))
        self.assertEqual(health.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(rebound_get.status, HTTPStatus.MISDIRECTED_REQUEST)
        self.assertEqual(rebound_post.status, HTTPStatus.MISDIRECTED_REQUEST)
        self.assertEqual(missing_origin.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(wrong_type.status, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        self.assertEqual(accepted.status, HTTPStatus.OK)
        self.assertTrue(accepted_body["ok"])

    def test_normalize_db_path_preserves_sqlite_uri(self) -> None:
        uri = "file:///tmp/entries.db?mode=ro"

        self.assertEqual(_normalize_db_path(uri), uri)

    def test_load_configured_sources_includes_journal_rss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conf_path = Path(td) / "config.toml"
            conf_path.write_text("""
                [[arXiv]]
                name = "Algebraic Geometry"
                url = "https://rss.arxiv.org/rss/math.AG"

                [[Journals]]
                name = "Annals of Mathematics"
                url = "https://annals.math.princeton.edu/feed"
            """)

            groups = serve._load_configured_sources(conf_path)

        self.assertEqual([group["name"] for group in groups], ["arXiv", "Journals"])
        self.assertEqual(groups[0]["children"][0]["selector_value"], "math.AG")
        self.assertEqual(
            groups[1]["children"][0]["selector_value"],
            "source:Journals:annals",
        )

    def test_cursor_round_trip_supports_long_ids_and_rejects_big_integers(self) -> None:
        position = (123, "arXiv", "x" * 400)

        self.assertEqual(_decode_cursor(_encode_cursor(position)), position)
        with self.assertRaisesRegex(ValueError, "invalid cursor"):
            _encode_cursor((2 ** 80, "arXiv", "2601.00001"))

class ServeMutationEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        with serve.REMOVE_UNDO_LOCK:
            serve.REMOVE_UNDO["token"] = None
            serve.REMOVE_UNDO["entry"] = None

    def test_favored_endpoint_updates_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": 1,
                    "expected_favored": 0,
                    "expected_revision": 0,
                },
            )

            harness._handle_favored()
            response = harness.response
            self.assertIsNotNone(response)
            self.assertEqual(harness.status, HTTPStatus.OK)
            self.assertTrue(response["ok"])
            self.assertEqual(response["updated"], 1)
            self.assertEqual(response["favored"], 1)

            with InfoStorage(str(db_path)) as storage:
                entries = storage.query_entries()["items"]
            self.assertEqual(entries[0]["favored"], 1)

    def test_entries_endpoint_filters_and_returns_cursor_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(db_path=db_path, payload=None)

            harness._handle_entries("q=Example+abstract&limit=1")

            response = harness.response
            self.assertEqual(harness.status, HTTPStatus.OK)
            self.assertEqual(response["total"], 1)
            self.assertEqual(len(response["items"]), 1)
            self.assertFalse(response["has_more"])
            self.assertIsNone(response["next_cursor"])

    def test_entries_endpoint_rejects_invalid_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = HandlerHarness(
                db_path=Path(td) / "entries.db",
                payload=None,
            )

            harness._handle_entries("cursor=not-base64!")

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)

    def test_entries_endpoint_rejects_oversized_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = HandlerHarness(
                db_path=Path(td) / "entries.db",
                payload=None,
            )

            harness._handle_entries(f"q={'x' * 501}")

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)

    def test_favored_endpoint_rejects_invalid_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": 2,
                    "expected_favored": 0,
                    "expected_revision": 0,
                },
            )

            harness._handle_favored()
            response = harness.response
            self.assertIsNotNone(response)
            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("favored", response["error"])

    def test_favored_endpoint_rejects_fractional_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": 1.9,
                    "expected_favored": 0,
                    "expected_revision": 0,
                },
            )

            harness._handle_favored()

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)

    def test_favored_endpoint_rejects_non_string_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": ["arXiv"],
                    "srce_id": {"id": "2601.00001"},
                    "favored": 1,
                    "expected_favored": 0,
                    "expected_revision": 0,
                },
            )

            harness._handle_favored()

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)

    def test_favored_endpoint_rejects_oversized_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": 1,
                    "expected_favored": 0,
                    "expected_revision": 2 ** 63,
                },
            )

            harness._handle_favored()

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)

    def test_favored_endpoint_rejects_stale_current_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            with InfoStorage(db_path) as storage:
                storage.favor_entry_if_current(
                    "arXiv", "2601.00001", 0, 0, 1
                )
            harness = HandlerHarness(
                db_path=db_path,
                payload={
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": 1,
                    "expected_favored": 0,
                    "expected_revision": 0,
                },
            )

            harness._handle_favored()

            self.assertEqual(harness.status, HTTPStatus.CONFLICT)

    def test_restore_endpoint_restores_removed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            with InfoStorage(db_path) as storage:
                newer = storage.query_entries()["items"][0]
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([], {
                        "https://example.com/feed": {"next_fetch_at": 999}
                    })
                newer["version"] = 2
                newer["updated"] = "2026-03-02T00:00:00+00:00"
                newer["content"]["titl"] = "Newer title"
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries([newer])
            remove_harness = HandlerHarness(
                db_path=db_path,
                payload={"srce_ty": "arXiv", "srce_id": "2601.00001"},
            )
            remove_harness._handle_remove_entry()
            response = remove_harness.response
            with InfoStorage(db_path) as storage:
                self.assertIn(
                    "https://example.com/feed",
                    storage.get_feed_states(),
                )
            harness = HandlerHarness(
                db_path=db_path,
                payload={"undo_token": response["undo_token"]},
            )

            harness._handle_restore_entry()

            self.assertEqual(harness.status, HTTPStatus.OK)
            with InfoStorage(db_path) as storage:
                restored = storage.query_entries()["items"][0]
            self.assertEqual(restored["version"], 2)
            self.assertEqual(restored["content"]["titl"], "Newer title")

    def test_restore_endpoint_rejects_invalid_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            harness = HandlerHarness(
                db_path=Path(td) / "entries.db",
                payload={"undo_token": "unknown"},
            )

            harness._handle_restore_entry()

            self.assertEqual(harness.status, HTTPStatus.CONFLICT)
