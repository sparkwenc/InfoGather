import tomllib
from dataclasses import dataclass
from pathlib import Path

from .sources import InfoSources
from .storage import InfoStorage


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


def run_ingestion(
    db_path: str | Path,
    config_path: str | Path,
) -> IngestionResult:
    config = load_config(config_path)
    with InfoStorage(db_path) as storage:
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
