import io
import os
import sqlite3
import statistics
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

from infogather.storage import InfoStorage


ENTRY_COUNT = 25_000
BUDGET_FLOOR_SECONDS = {
    "first_page": 0.15,
    "search": 2.00,
    "facets": 1.50,
    "search_facets": 4.00,
}
BUDGET_SCAN_MULTIPLIER = {
    "first_page": 5,
    "search": 50,
    "facets": 50,
    "search_facets": 100,
}


def _median_seconds(operation) -> float:
    operation()
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


class StoragePerformanceTests(unittest.TestCase):
    def test_25k_query_budget(self) -> None:
        temp_root = os.environ.get("INFOGATHER_PERF_DIR")
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            db_path = Path(directory) / "entries.db"
            abstract = " ".join([
                "Representative abstract text covering methods, results, and context."
            ] * 16)
            with InfoStorage(db_path) as storage:
                entries = [
                    {
                        "srce_ty": "arXiv",
                        "srce_id": f"2601.{index:05d}",
                        "version": 1,
                        "favored": int(index % 20 == 0),
                        "noticed": int(index % 3 == 0),
                        "updated": f"2026-03-{index % 28 + 1:02d}T00:00:00+00:00",
                        "content": {
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
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries(entries)

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
                "first_page": lambda: query("query_entries", limit=24),
                "search": lambda: query(
                    "query_entries",
                    query_text="needle",
                    limit=24,
                ),
                "facets": lambda: query(
                    "query_facets",
                    configured_tags=tags,
                    groups=groups,
                    selected_tags=set(),
                ),
                "search_facets": lambda: query(
                    "query_facets",
                    configured_tags=tags,
                    groups=groups,
                    selected_tags=set(),
                    query_text="needle",
                ),
            }
            self.assertEqual(operations["first_page"]()["total"], ENTRY_COUNT)
            self.assertEqual(operations["search"]()["total"], ENTRY_COUNT // 10)
            self.assertEqual(operations["facets"]()["total"], ENTRY_COUNT)
            self.assertEqual(
                operations["search_facets"]()["total"],
                ENTRY_COUNT // 10,
            )
            scan_seconds = _median_seconds(raw_scan)
            timings = {
                name: _median_seconds(operation)
                for name, operation in operations.items()
            }

        print(f"25k raw scan: {scan_seconds * 1000:.1f}ms; timings:", {
            name: f"{seconds * 1000:.1f}ms"
            for name, seconds in timings.items()
        })
        for name, seconds in timings.items():
            budget = max(
                BUDGET_FLOOR_SECONDS[name],
                scan_seconds * BUDGET_SCAN_MULTIPLIER[name],
            )
            self.assertLess(
                seconds,
                budget,
                f"{name} took {seconds:.3f}s (budget {budget:.3f}s)",
            )
