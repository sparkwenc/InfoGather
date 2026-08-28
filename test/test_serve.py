import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from http import HTTPStatus

from infogather.serve import (
    InfoHandler,
    InfoStorage,
    _decode_cursor,
    _encode_cursor,
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
        result = IngestionResult(2, 1, 0, 3, 2)
        with mock.patch.object(
            serve, "run_ingestion", return_value=result
        ) as ingestion:
            db_path = Path("/tmp/custom-entries.db")
            conf_path = Path("/tmp/custom-config.toml")
            _run_ins_job(db_path, conf_path)

        ingestion.assert_called_once_with(db_path, conf_path)
        self.assertEqual(serve.INS_JOB["state"], "succeeded")
        self.assertEqual(serve.INS_JOB["message"], "拉取完成: 2/3 条更新")

    def test_web_assets_are_packaged_with_server(self) -> None:
        self.assertTrue((serve.WEB_DIR / "index.html").is_file())

    def test_normalize_db_path_preserves_sqlite_uri(self) -> None:
        uri = "file:///tmp/entries.db?mode=ro"

        self.assertEqual(_normalize_db_path(uri), uri)

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

    def test_favored_endpoint_rejects_stale_current_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            with InfoStorage(db_path) as storage:
                storage.favor_entry("arXiv", "2601.00001", 1)
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
                storage.update_feed_states(
                    {"https://example.com/feed": {"next_fetch_at": 999}}
                )
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
