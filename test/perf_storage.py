import io
import os
import statistics
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from infogather.storage import InfoStorage


ENTRY_COUNT = 25_000
BUDGET_SECONDS = {
    "first_page": 0.10,
    "search": 1.00,
    "facets": 0.75,
    "search_facets": 3.00,
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
            with InfoStorage(db_path) as storage:
                entries = [
                    {
                        "srce_ty": "arXiv",
                        "srce_id": f"2601.{index:05d}",
                        "version": 1,
                        "favored": index % 20 == 0,
                        "noticed": index % 3 == 0,
                        "updated": f"2026-03-{index % 28 + 1:02d}T00:00:00+00:00",
                        "content": {
                            "titl": f"Paper {index}",
                            "auth": f"Author {index % 100}",
                            "abst": "needle result" if index % 10 == 0 else "ordinary",
                            "tags": [f"math.{index % 8}"],
                        },
                    }
                    for index in range(ENTRY_COUNT)
                ]
                with redirect_stdout(io.StringIO()):
                    storage.insert_entries(entries)

                tags = {f"math.{index}" for index in range(8)}
                groups = [{f"math.{index}"} for index in range(8)]
                operations = {
                    "first_page": lambda: storage.query_entries(limit=24),
                    "search": lambda: storage.query_entries(
                        query_text="needle", limit=24
                    ),
                    "facets": lambda: storage.query_facets(
                        configured_tags=tags,
                        groups=groups,
                        selected_tags=set(),
                    ),
                    "search_facets": lambda: storage.query_facets(
                        configured_tags=tags,
                        groups=groups,
                        selected_tags=set(),
                        query_text="needle",
                    ),
                }
                timings = {
                    name: _median_seconds(operation)
                    for name, operation in operations.items()
                }

        print("25k storage timings:", {
            name: f"{seconds * 1000:.1f}ms"
            for name, seconds in timings.items()
        })
        for name, seconds in timings.items():
            self.assertLess(
                seconds,
                BUDGET_SECONDS[name],
                f"{name} took {seconds:.3f}s (budget {BUDGET_SECONDS[name]:.3f}s)",
            )
