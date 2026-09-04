import io
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing, redirect_stdout
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from infogather.paths import WEB_DIR
from infogather.serve import InfoHandler
from infogather.storage import InfoStorage


ENTRY_COUNT = 25_000
SAMPLES = 3
DATABASE_BUDGET_FLOOR_SECONDS = {
    "db_bulk_insert_25k": 1.500,
    "db_flag_write": 0.005,
    "db_first_page": 0.005,
    "db_search": 0.080,
    "db_facets": 0.100,
    "db_search_facets": 0.100,
    "api_entries": 0.020,
    "api_entries_keepalive": 0.002,
    "api_facets": 0.120,
    "api_health_cold": 0.005,
    "api_health_keepalive": 0.001,
    "api_search_facets": 0.120,
    "api_flag_write": 0.010,
    "api_flag_write_keepalive": 0.003,
}
DATABASE_BUDGET_SCAN_MULTIPLIER = {
    "db_bulk_insert_25k": 40,
    "db_flag_write": 0.15,
    "db_first_page": 0.15,
    "db_search": 2.5,
    "db_facets": 3,
    "db_search_facets": 3,
    "api_entries": 0.5,
    "api_entries_keepalive": 0.06,
    "api_facets": 3.5,
    "api_health_cold": 0.1,
    "api_health_keepalive": 0.02,
    "api_search_facets": 3.5,
    "api_flag_write": 0.3,
    "api_flag_write_keepalive": 0.08,
}
FRONTEND_BUDGETS = {
    "frontend_cards_1000_ms": 7.0,
    "frontend_trees_1000_ms": 15.0,
    "frontend_status_1000_ms": 2.0,
    "frontend_assets_bytes": 60_000,
}


def _median_seconds(operation, *, samples: int = SAMPLES) -> float:
    operation()
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        timings.append(time.perf_counter() - started)
    return statistics.median(timings)


class _QuietHandler(InfoHandler):
    def log_message(self, *_args) -> None:
        pass


class PerformanceBaselineTests(unittest.TestCase):
    def test_application_performance_baseline(self) -> None:
        temp_root = os.environ.get("INFOGATHER_PERF_DIR")
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            root = Path(directory)
            db_path = root / "entries.db"
            config_path = root / "config.toml"
            abstract = " ".join([
                "Representative abstract text covering methods, results, and context."
            ] * 16)
            entries = [
                {
                    "srce_ty": "arXiv",
                    "srce_id": f"2601.{index:05d}",
                    "version": 1,
                    "favored": int(index % 20 == 0),
                    "noticed": int(index % 3 == 0),
                    "updated": f"2026-03-{index % 28 + 1:02d}T00:00:00+00:00",
                    "content": {
                        "link": f"https://arxiv.org/abs/2601.{index:05d}",
                        "titl": f"Paper {index}",
                        "auth": f"Author {index % 100}",
                        "abst": (
                            f"needle result {abstract}"
                            if index % 10 == 0 else abstract
                        ),
                        "tags": [
                            f"math.{index % 16}",
                            f"area.{index % 4}",
                            "arXiv",
                        ],
                    },
                }
                for index in range(ENTRY_COUNT)
            ]
            started = time.perf_counter()
            with InfoStorage(db_path) as storage, redirect_stdout(io.StringIO()):
                storage.insert_entries(entries)
            measurements = {
                "db_bulk_insert_25k": time.perf_counter() - started,
            }
            config_path.write_text("\n".join(
                f'[[arXiv]]\nname = "Source {index}"\n'
                f'url = "https://rss.arxiv.org/rss/math.{index}"\n'
                for index in range(16)
            ))

            tags = {f"math.{index}" for index in range(16)}
            groups = [tags]

            def query(method, **kwargs):
                with InfoStorage.open_current(db_path) as storage:
                    return getattr(storage, method)(**kwargs)

            def raw_scan():
                with closing(sqlite3.connect(db_path)) as connection:
                    return connection.execute(
                        "SELECT sum(length(content)) FROM tab_entries"
                    ).fetchone()[0]

            operations = {
                "db_first_page": lambda: query("query_entries", limit=24),
                "db_search": lambda: query(
                    "query_entries",
                    query_text="needle",
                    limit=24,
                ),
                "db_facets": lambda: query(
                    "query_facets",
                    configured_tags=tags,
                    groups=groups,
                    selected_tags=set(),
                ),
                "db_search_facets": lambda: query(
                    "query_facets",
                    configured_tags=tags,
                    groups=groups,
                    selected_tags=set(),
                    query_text="needle",
                ),
            }
            self.assertEqual(operations["db_first_page"]()["total"], ENTRY_COUNT)
            self.assertEqual(
                operations["db_search"]()["total"],
                ENTRY_COUNT // 10,
            )
            self.assertEqual(operations["db_facets"]()["total"], ENTRY_COUNT)
            self.assertEqual(
                operations["db_search_facets"]()["total"],
                ENTRY_COUNT // 10,
            )
            raw_scan_seconds = _median_seconds(raw_scan)
            measurements.update({
                name: _median_seconds(operation)
                for name, operation in operations.items()
            })

            favored = 0
            revision = 0

            def write_flags() -> None:
                nonlocal favored, revision
                for _ in range(20):
                    next_favored = 1 - favored
                    with InfoStorage.open_current(db_path) as storage:
                        changed = storage.favor_entry_if_current(
                            "arXiv",
                            "2601.00001",
                            favored,
                            revision,
                            next_favored,
                        )
                    if changed != 1:
                        raise AssertionError("flag baseline lost its target row")
                    favored = next_favored
                    revision += 1

            measurements["db_flag_write"] = _median_seconds(write_flags) / 20
            measurements.update(self._measure_api(
                db_path,
                config_path,
                initial_revision=revision,
                initial_favored=favored,
            ))

        frontend = self._measure_frontend()
        measurements_ms = {
            name: round(seconds * 1000, 3)
            for name, seconds in measurements.items()
        }
        measurements_ms.update(frontend)
        first_party_assets = [
            WEB_DIR / "index.html",
            *sorted((WEB_DIR / "css").glob("*.css")),
            *sorted((WEB_DIR / "js").glob("*.js")),
        ]
        measurements_ms["frontend_assets_bytes"] = sum(
            path.stat().st_size for path in first_party_assets
        )
        budgets_ms = {
            name: round(max(
                DATABASE_BUDGET_FLOOR_SECONDS[name],
                raw_scan_seconds * DATABASE_BUDGET_SCAN_MULTIPLIER[name],
            ) * 1000, 3)
            for name in DATABASE_BUDGET_FLOOR_SECONDS
        }
        budgets_ms.update(FRONTEND_BUDGETS)

        print("Performance baseline:", json.dumps({
            "measurements": measurements_ms,
            "budgets": budgets_ms,
            "calibration_raw_scan_ms": round(raw_scan_seconds * 1000, 3),
        }, sort_keys=True))
        for name, budget in budgets_ms.items():
            self.assertLess(
                measurements_ms[name],
                budget,
                f"{name} regressed: {measurements_ms[name]} >= {budget}",
            )

    def _measure_api(
        self,
        db_path: Path,
        config_path: Path,
        *,
        initial_revision: int,
        initial_favored: int,
    ) -> dict[str, float]:
        handler = partial(
            _QuietHandler,
            db_path=db_path,
            conf_path=config_path,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        keepalive = HTTPConnection(*server.server_address, timeout=5)

        def request(method: str, path: str, payload: dict | None = None) -> dict:
            body = None if payload is None else json.dumps(payload)
            headers = {}
            if body is not None:
                headers = {"Content-Type": "application/json", "Origin": origin}
            connection = HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                result = json.loads(response.read())
            finally:
                connection.close()
            if response.status != 200:
                raise AssertionError(f"API baseline failed: {response.status} {result}")
            return result

        def keepalive_request(
            method: str,
            path: str,
            payload: dict | None = None,
        ) -> dict:
            body = None if payload is None else json.dumps(payload)
            headers = {}
            if body is not None:
                headers = {"Content-Type": "application/json", "Origin": origin}
            keepalive.request(method, path, body=body, headers=headers)
            response = keepalive.getresponse()
            result = json.loads(response.read())
            if response.status != 200 or response.version != 11:
                raise AssertionError(
                    f"keep-alive baseline failed: {response.status} {result}"
                )
            return result

        favored = initial_favored
        revision = initial_revision

        def write_flags(send=request) -> None:
            nonlocal favored, revision
            for _ in range(10):
                next_favored = 1 - favored
                send("POST", "/api/favored", {
                    "srce_ty": "arXiv",
                    "srce_id": "2601.00001",
                    "favored": next_favored,
                    "expected_favored": favored,
                    "expected_revision": revision,
                })
                favored = next_favored
                revision += 1

        try:
            measurements = {
                "api_entries": _median_seconds(
                    lambda: request("GET", "/api/entries?limit=24&include_total=1")
                ),
                "api_entries_keepalive": _median_seconds(
                    lambda: keepalive_request(
                        "GET", "/api/entries?limit=24&include_total=1"
                    )
                ),
                "api_facets": _median_seconds(
                    lambda: request("GET", "/api/tag-tree")
                ),
                "api_health_cold": _median_seconds(
                    lambda: request("GET", "/api/health")
                ),
                "api_health_keepalive": _median_seconds(
                    lambda: keepalive_request("GET", "/api/health")
                ),
                "api_search_facets": _median_seconds(
                    lambda: request("GET", "/api/tag-tree?q=needle")
                ),
                "api_flag_write": _median_seconds(write_flags) / 10,
                "api_flag_write_keepalive": _median_seconds(
                    lambda: write_flags(keepalive_request)
                ) / 10,
            }
        finally:
            keepalive.close()
            server.shutdown()
            server.server_close()
            thread.join()
        return measurements

    def _measure_frontend(self) -> dict[str, float]:
        node = shutil.which("node")
        if node is None:
            self.fail("Node.js is required for the frontend performance baseline")
        result = subprocess.run(
            [node, Path(__file__).with_name("perf_web.mjs")],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
