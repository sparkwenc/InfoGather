from sources import InfoSources
from storage import InfoStorage
from filters import InfoFilters


conf = [
    "https://rss.arxiv.org/rss/math.AG",
    "https://rss.arxiv.org/rss/math.AC",
]


sources = InfoSources(conf)
with InfoStorage("entries.db") as storage:
    compressed_entries = sources.normalized_feeds_arxiv()

    tot, ins, upd = storage.insert_to_db(compressed_entries)
    print(f"Total: {tot}, Inserted: {ins}, Updated: {upd}")

    le = storage.export_entries("feb23.md", InfoFilters.filter_ingestion)
    print(f"Exported {le} entries updated in the last day.")
