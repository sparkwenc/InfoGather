import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock

from http import HTTPStatus

from infogather.serve import (
    InfoHandler,
    InfoStorage,
    _decode_cursor,
    _encode_cursor,
    _normalize_db_path,
    _ins_progress_from_line,
    _run_ins_job,
)
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


class HandlerHarness:
    _source_key_from_payload = staticmethod(InfoHandler._source_key_from_payload)
    _binary_value_from_payload = staticmethod(InfoHandler._binary_value_from_payload)
    _revision_from_payload = staticmethod(InfoHandler._revision_from_payload)
    _handle_entry_mutation = InfoHandler._handle_entry_mutation
    _handle_entries = InfoHandler._handle_entries
    _handle_favored = InfoHandler._handle_favored
    _handle_remove_entry = InfoHandler._handle_remove_entry
    _handle_restore_entry = InfoHandler._handle_restore_entry

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
            serve.INS_JOB["returncode"] = None
            serve.INS_JOB["logs"] = []

    def test_run_ins_job_passes_database_and_config_paths(self) -> None:
        captured: dict[str, object] = {}

        class _FakeProc:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.stdout = []

            def wait(self) -> int:
                return 0

        with mock.patch.object(serve.subprocess, "Popen", side_effect=_FakeProc):
            db_path = Path("/tmp/custom-entries.db")
            conf_path = Path("/tmp/custom-config.toml")
            _run_ins_job(db_path, conf_path)

        self.assertEqual(
            captured["cmd"],
            [
                sys.executable,
                "-m",
                "infogather.cli",
                "--db-path",
                str(db_path),
                "ins",
                "--conf",
                str(conf_path),
            ],
        )

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

    def test_source_progress_line_reports_completed_feeds(self) -> None:
        progress, message = _ins_progress_from_line(
            "SOURCE 4/17: 20 from Example",
            8,
        )

        self.assertEqual(progress, 21)
        self.assertEqual(message, "抓取源 4/17")

    def test_run_ins_job_reports_partial_failure(self) -> None:
        class _FakeProc:
            stdout = [
                "SOURCE 1/2: failed Example: timeout\n",
                "Fetch result: 10 entries, 0 cached, 1 failed from 2 feeds\n",
            ]

            def __init__(self, *args, **kwargs):
                pass

            def wait(self) -> int:
                return 0

        with mock.patch.object(serve.subprocess, "Popen", side_effect=_FakeProc):
            _run_ins_job(Path("/tmp/entries.db"), Path("/tmp/config.toml"))

        self.assertEqual(serve.INS_JOB["state"], "succeeded")
        self.assertEqual(serve.INS_JOB["message"], "拉取完成，部分源失败")


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
            response = cast(dict, response)
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

            response = cast(dict, harness.response)
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
            response = cast(dict, response)
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
            response = cast(dict, remove_harness.response)
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
