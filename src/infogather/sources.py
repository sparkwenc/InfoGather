import time
import http.client
import feedparser
import calendar
from datetime import datetime, timezone


class InfoSources:
    def __init__(self, conf: dict) -> None:
        self._conf = conf

    def get_normalized_feeds(self) -> list:
        """return normalized entries from all sources"""
        raw_feeds = self._fetch_raw_feeds()

        print("Normalizing feeds...")
        normalized = []

        cnt_n = 0
        for srce_ty, feeds in raw_feeds.items():
            fn = getattr(self, f"_normalized_{srce_ty}", None)
            if fn is None or not callable(fn):
                print(f"{srce_ty:5s}: unknown source type, skipped")
                continue
            result = fn(feeds)
            cnt_n += len(result)
            print(f"{srce_ty:5s}: {len(result):3d} normalized")
            normalized.extend(result)
        print(f"Total: {cnt_n:3d} normalized\n")
        return normalized

    # internal methods
    def _fetch_raw_feeds(self) -> dict:
        """fetch all feeds according to the configuration"""

        cnt_f = len(self._conf)
        tot_f = 0
        print(f"Fetching feeds from {cnt_f} source types...")
        feeds = {}
        for ind_f, [srce_ty, lists] in enumerate(self._conf.items()):
            feeds[srce_ty] = []
            cnt = len(lists)
            tot = 0
            print(
                f"{ind_f + 1:2d}/{cnt_f:2d}-{srce_ty:5s}: Fetching from {cnt} sources...")
            for ind, item in enumerate(lists):
                name = str(item.get("name", "")).strip() or "unknown"
                feed = self._fetch_feed(item["url"], name)
                feeds[srce_ty].append(feed)
                inc = len(feed.get("entries", []))
                tot += inc
                print(
                    f"      {ind + 1:2d}/{cnt:2d}: {inc:3d} from {name}")
            print(f"      Total: {tot:3d} entries from {cnt} sources")
            tot_f += tot
        print(f"Total: {tot_f:3d} entries from {cnt_f} source types\n")
        return feeds

    @staticmethod
    def _fetch_feed(url: str, name: str, attempts: int = 3, delay: float = 1.0) -> dict:
        for attempt in range(1, attempts + 1):
            try:
                return feedparser.parse(url)
            except http.client.IncompleteRead as exc:
                if attempt >= attempts:
                    raise
                print(
                    f"      {name}: incomplete response "
                    f"({len(exc.partial)} bytes read, {exc.expected} more expected), "
                    f"retrying {attempt + 1}/{attempts}")
                time.sleep(delay)

        raise RuntimeError("unreachable feed fetch state")

    @staticmethod
    def _feed_updated_iso(feed: dict) -> str:
        feed_meta = feed.get("feed", {})
        candidates = [
            feed_meta.get("updated_parsed"),
            feed_meta.get("published_parsed"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                st = time.struct_time(candidate)
                return datetime.fromtimestamp(
                    calendar.timegm(st),
                    timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                continue
        return datetime.now(timezone.utc).isoformat()

    def _normalized_arXiv(self, feeds: list) -> list:
        """normalized arXiv feeds"""

        normalized = []
        for feed in feeds:
            entries = feed.get("entries", [])
            dt = self._feed_updated_iso(feed)

            for entry in entries:
                id, _, ver = entry.get("id").split(":")[-1].rpartition("v")
                _, _, abstract = entry.get("summary").partition("\nAbstract: ")
                tags = [tag.get("term") for tag in entry.get("tags", [])]

                normalized.append({
                    "srce_ty": "arXiv",
                    "srce_id": id,
                    "version": int(ver),
                    "favored": 0,
                    "noticed": 0,
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

    def _normalized_AMS(self, feeds: list) -> list:
        # TODO: implement AMS normalization
        return []
