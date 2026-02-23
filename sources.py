import time
import feedparser
import calendar
from datetime import datetime, timezone


class InfoSources:
    def __init__(self, conf: dict) -> None:
        self._conf = conf

    # internal methods
    def _fetch_feeds(self) -> list:
        feeds = []

        n = len(self._conf)
        cnt = 0
        for ind, [url, abbrev] in enumerate(self._conf.items()):
            feed = feedparser.parse(url)
            inc = len(feed.get("entries", []))
            cnt += inc

            feeds.append(feed)
            print(f"{ind + 1:2d}/{n:2d}: {inc:3d} from {abbrev}")
        print(f"Source result: {cnt:3d} from {n} sources.")
        return feeds

    def normalized_feeds_arxiv(self) -> list:
        normalized = []
        for feed in self._fetch_feeds():
            entries = feed.get("entries", [])
            st = time.struct_time(
                feed.get("feed", {}).get("updated_parsed", []))
            dt = datetime.fromtimestamp(
                calendar.timegm(st), timezone.utc).isoformat()

            for entry in entries:
                id, _, ver = entry.get("id").split(":")[-1].rpartition("v")
                _, _, abstract = entry.get("summary").partition("\nAbstract: ")
                tags = [tag.get("term") for tag in entry.get("tags", [])]

                normalized.append({
                    "srce_ty": "arXiv",
                    "srce_id": id,
                    "version": int(ver),
                    "favored": 0,
                    "updated": dt,
                    "content": {
                        "link": entry.get("link"),
                        "titl": entry.get("title"),
                        "auth": entry.get("author"),
                        "abst": abstract,
                        "tags": tags,
                    },
                })
        return normalized
