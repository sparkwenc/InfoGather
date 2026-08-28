import fcntl
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from .sources import InfoSources
from .storage import InfoStorage


_MEMORY_LOCKS: dict[str, threading.Lock] = {}
_MEMORY_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class IngestionResult:
    total_feeds: int
    cached_feeds: int
    failed_feeds: int
    normalized_entries: int
    changed_entries: int


def load_config(path: str | Path) -> dict:
    with Path(path).expanduser().open("rb") as config_file:
        return tomllib.load(config_file)


@contextmanager
def _ingestion_lock(db_path: str | Path):
    raw_path = str(db_path)
    if raw_path == ":memory:":
        yield
        return
    parsed = urlparse(raw_path)
    query = parse_qsl(parsed.query)
    if raw_path.startswith("file:") and dict(query).get("mode") == "memory":
        memory_key = parsed._replace(
            query=urlencode(sorted(query))
        ).geturl()
        with _MEMORY_LOCKS_GUARD:
            lock = _MEMORY_LOCKS.setdefault(memory_key, threading.Lock())
        with lock:
            yield
        return
    if raw_path.startswith("file:"):
        raw_path = unquote(urlparse(raw_path).path)
    database_path = Path(raw_path).expanduser().resolve()
    lock_path = database_path.with_suffix(
        database_path.suffix + ".ingest.lock"
    )
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def run_ingestion(
    db_path: str | Path,
    config_path: str | Path,
) -> IngestionResult:
    config = load_config(config_path)
    with InfoStorage(db_path) as storage:
        with _ingestion_lock(db_path):
            sources = InfoSources(config, feed_states=storage.get_feed_states())
            entries = sources.get_normalized_feeds()
            changed = storage.insert_entries(entries, sources.feed_state_updates)
    return IngestionResult(
        total_feeds=sources.total_feeds,
        cached_feeds=sources.cached_feeds,
        failed_feeds=sources.failed_feeds,
        normalized_entries=len(entries),
        changed_entries=changed,
    )
