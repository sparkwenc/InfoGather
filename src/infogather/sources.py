import time
import http.client
import feedparser
import calendar
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


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
        if attempts < 1:
            raise ValueError("attempts must be at least 1")

        for attempt in range(1, attempts + 1):
            try:
                feed = feedparser.parse(url)
            except http.client.IncompleteRead as exc:
                if attempt >= attempts:
                    raise
                print(
                    f"      {name}: incomplete response "
                    f"({len(exc.partial)} bytes read, {exc.expected} more expected), "
                    f"retrying {attempt + 1}/{attempts}")
                time.sleep(delay)
                continue

            status = feed.get("status")
            if isinstance(status, int) and status >= 400:
                error = RuntimeError(
                    f"{name}: feed request failed with HTTP {status}"
                )
            elif feed.get("bozo"):
                detail = feed.get("bozo_exception", "parse error")
                error = RuntimeError(f"{name}: invalid feed: {detail}")
            else:
                return feed

            if attempt >= attempts:
                raise error
            print(
                f"      {error}, retrying {attempt + 1}/{attempts}"
            )
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
                try:
                    srce_id, version = self._parse_arxiv_id(entry.get("id"))
                except (TypeError, ValueError) as exc:
                    print(f"arXiv: malformed entry skipped: {exc}")
                    continue

                summary = str(entry.get("summary") or "").strip()
                parts = re.split(
                    r"(?:^|\r?\n)\s*Abstract:\s*",
                    summary,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )
                abstract = parts[-1].strip()
                tags = []
                for tag in entry.get("tags", []) or []:
                    if not isinstance(tag, dict):
                        continue
                    term = tag.get("term")
                    if isinstance(term, str) and term.strip():
                        tags.append(term.strip())

                normalized.append({
                    "srce_ty": "arXiv",
                    "srce_id": srce_id,
                    "version": version,
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

    @staticmethod
    def _parse_arxiv_id(raw_id: object) -> tuple[str, int]:
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValueError("missing arXiv id")

        value = raw_id.strip()
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            value = parsed.path
        elif value.lower().startswith("oai:"):
            value = value.rsplit(":", 1)[-1]
        elif value.lower().startswith("arxiv:"):
            value = value.split(":", 1)[-1]

        value = value.strip("/")
        for prefix in ("abs/", "pdf/"):
            if value.lower().startswith(prefix):
                value = value[len(prefix):]
                break
        if value.lower().endswith(".pdf"):
            value = value[:-4]

        srce_id, separator, raw_version = value.rpartition("v")
        if not separator or not srce_id or not raw_version.isdigit():
            raise ValueError(f"invalid arXiv id: {raw_id!r}")
        return srce_id, int(raw_version)
