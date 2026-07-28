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
    _normalize_db_path,
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
    _handle_entry_mutation = InfoHandler._handle_entry_mutation
    _handle_favored = InfoHandler._handle_favored

    def __init__(self, db_path: Path, payload: dict | None) -> None:
        self._db_path = db_path
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


class ServeMutationEndpointTests(unittest.TestCase):
    def test_favored_endpoint_updates_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={"srce_ty": "arXiv", "srce_id": "2601.00001", "favored": 1},
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
                entries = storage.export_entries_json()
            self.assertEqual(entries[0]["favored"], 1)

    def test_favored_endpoint_rejects_invalid_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "entries.db"
            _seed_entry(db_path)
            harness = HandlerHarness(
                db_path=db_path,
                payload={"srce_ty": "arXiv", "srce_id": "2601.00001", "favored": 2},
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
                payload={"srce_ty": "arXiv", "srce_id": "2601.00001", "favored": 1.9},
            )

            harness._handle_favored()

            self.assertEqual(harness.status, HTTPStatus.BAD_REQUEST)
