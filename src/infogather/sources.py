import time
import http.client
import feedparser
import calendar
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx


class InfoSources:
    def __init__(
        self,
        conf: dict,
        feed_states: dict[str, dict] | None = None,
        max_workers: int = 8,
    ) -> None:
        self._conf = conf
        self._feed_states = feed_states or {}
        self._max_workers = max(1, max_workers)
        self.feed_state_updates: dict[str, dict] = {}
        self.failed_count = 0

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
        deduplicated = self._deduplicate_entries(normalized)
        duplicate_count = len(normalized) - len(deduplicated)
        if duplicate_count:
            print(f"Deduplicated: {duplicate_count:3d} cross-listed entries")
        print(f"Total: {len(deduplicated):3d} normalized\n")
        return deduplicated

    # internal methods
    def _fetch_raw_feeds(self) -> dict:
        """fetch all feeds according to the configuration"""

        feeds: dict[str, list] = {}
        requests = []
        seen_urls = set()
        for srce_ty, raw_sources in self._conf.items():
            normalizer = getattr(self, f"_normalized_{srce_ty}", None)
            if not callable(normalizer):
                print(f"{srce_ty:5s}: unknown source type, skipped")
                continue
            if not isinstance(raw_sources, list):
                raise ValueError(f"source group {srce_ty!r} must be a list")
            feeds[srce_ty] = []
            for item in raw_sources:
                if not isinstance(item, dict):
                    raise ValueError(f"source in {srce_ty!r} must be a table")
                name = str(item.get("name", "")).strip() or "unknown"
                url = str(item.get("url", "")).strip()
                if not url:
                    raise ValueError(f"source {name!r} has no URL")
                if url in seen_urls:
                    print(f"      duplicate URL skipped: {name}")
                    continue
                seen_urls.add(url)
                requests.append((srce_ty, name, url))

        total = len(requests)
        print(
            f"Fetching {total} feeds with up to "
            f"{min(self._max_workers, max(total, 1))} concurrent requests..."
        )
        if not requests:
            return feeds

        now = time.time()
        pending = []
        completed = 0
        cached = 0
        failed = 0
        total_entries = 0
        for srce_ty, name, url in requests:
            state = self._feed_states.get(url, {})
            if float(state.get("next_fetch_at", 0) or 0) > now:
                completed += 1
                cached += 1
                print(f"SOURCE {completed}/{total}: cached {name}")
                continue
            pending.append((srce_ty, name, url, state))

        limits = httpx.Limits(
            max_connections=self._max_workers,
            max_keepalive_connections=self._max_workers,
        )
        timeout = httpx.Timeout(12.0, connect=4.0)
        with httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "InfoGather/0.1 (+local RSS reader)"},
            limits=limits,
            timeout=timeout,
        ) as client, ThreadPoolExecutor(
            max_workers=min(self._max_workers, max(len(pending), 1))
        ) as executor:
            futures = {
                executor.submit(
                    self._fetch_feed,
                    client,
                    url,
                    name,
                    state,
                ): (srce_ty, name, url)
                for srce_ty, name, url, state in pending
            }
            for future in as_completed(futures):
                srce_ty, name, url = futures[future]
                completed += 1
                try:
                    feed, state_update = future.result()
                    self.feed_state_updates[url] = state_update
                    if feed is None:
                        cached += 1
                        print(f"SOURCE {completed}/{total}: not modified {name}")
                        continue
                    count = len(feed.get("entries", []))
                    feeds[srce_ty].append(feed)
                    total_entries += count
                    print(f"SOURCE {completed}/{total}: {count:3d} from {name}")
                except Exception as exc:
                    failed += 1
                    print(f"SOURCE {completed}/{total}: failed {name}: {exc}")

        print(
            f"Fetch result: {total_entries} entries, {cached} cached, "
            f"{failed} failed from {total} feeds\n"
        )
        self.failed_count = failed
        if failed == total and not cached:
            raise RuntimeError("all configured feeds failed")
        return feeds

    @staticmethod
    def _fetch_feed(
        client: httpx.Client,
        url: str,
        name: str,
        state: dict | None = None,
        attempts: int = 2,
        delay: float = 0.5,
    ) -> tuple[dict | None, dict]:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")

        state = state or {}
        headers = {}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])

        for attempt in range(1, attempts + 1):
            try:
                response = client.get(url, headers=headers)
            except (http.client.IncompleteRead, httpx.TransportError) as exc:
                if isinstance(exc, httpx.TimeoutException):
                    raise
                if attempt >= attempts:
                    raise
                print(
                    f"      {name}: transport error ({exc}), "
                    f"retrying {attempt + 1}/{attempts}"
                )
                time.sleep(delay)
                continue

            state_update = InfoSources._feed_state_from_headers(
                response.headers,
                previous=state,
                status_code=response.status_code,
            )
            if response.status_code == 304:
                return None, state_update

            if response.status_code >= 400:
                error = RuntimeError(
                    f"{name}: feed request failed with HTTP {response.status_code}"
                )
                retryable = (
                    response.status_code in {408, 425, 429}
                    or response.status_code >= 500
                )
                if not retryable or attempt >= attempts:
                    raise error
                print(f"      {error}, retrying {attempt + 1}/{attempts}")
                time.sleep(delay)
                continue

            feed = feedparser.parse(
                response.content,
                response_headers=dict(response.headers),
            )
            if feed.get("bozo"):
                detail = feed.get("bozo_exception", "parse error")
                raise RuntimeError(f"{name}: invalid feed: {detail}")
            return feed, state_update

        raise RuntimeError("unreachable feed fetch state")

    @staticmethod
    def _feed_state_from_headers(
        headers: object,
        previous: dict,
        *,
        status_code: int | None = None,
    ) -> dict:
        get_header = getattr(headers, "get")
        cache_control = str(get_header("cache-control", ""))
        directives = cache_control.lower()
        match = re.search(r"(?:^|,)\s*max-age=(\d+)", directives)
        if match is None:
            match = re.search(r"(?:^|,)\s*s-maxage=(\d+)", directives)
        max_age = min(int(match.group(1)), 86_400) if match else 0
        no_store = "no-store" in directives
        no_cache = "no-cache" in directives
        if no_cache or no_store:
            max_age = 0
        try:
            age = max(0, int(get_header("age", "0")))
        except (TypeError, ValueError):
            age = 0
        fresh_for = max(0, max_age - age)
        if (
            fresh_for == 0
            and not no_cache
            and not no_store
            and status_code == 304
        ):
            # 304 without any freshness directive: the resource is
            # unchanged; revalidate after a sane default interval
            # instead of on every single run.
            fresh_for = 1800
        return {
            "etag": None if no_store else (
                get_header("etag") or previous.get("etag")
            ),
            "last_modified": None if no_store else (
                get_header("last-modified") or previous.get("last_modified")
            ),
            "next_fetch_at": time.time() + fresh_for,
        }

    @staticmethod
    def _deduplicate_entries(entries: list[dict]) -> list[dict]:
        unique: dict[tuple[str, str], dict] = {}
        for entry in entries:
            key = (entry["srce_ty"], entry["srce_id"])
            existing = unique.get(key)
            if existing is None or entry["version"] > existing["version"]:
                unique[key] = entry
                continue
            if entry["version"] != existing["version"]:
                continue
            existing_tags = existing["content"].get("tags", [])
            incoming_tags = entry["content"].get("tags", [])
            existing["content"]["tags"] = list(
                dict.fromkeys([*existing_tags, *incoming_tags])
            )
        return list(unique.values())

    @staticmethod
    def _feed_updated_iso(feed: dict) -> str:
        feed_meta = feed.get("feed", {})
        iso = InfoSources._first_parsed_iso(
            feed_meta.get("updated_parsed"),
            feed_meta.get("published_parsed"),
        )
        return iso or datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _entry_updated_iso(entry: dict, feed_fallback: str) -> str:
        """Per-entry update timestamp (paper announcement date), falling
        back to the feed timestamp when the entry carries no date."""
        iso = InfoSources._first_parsed_iso(
            entry.get("updated_parsed"),
            entry.get("published_parsed"),
        )
        return iso or feed_fallback

    @staticmethod
    def _first_parsed_iso(*candidates: object) -> str | None:
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
        return None

    def _normalized_arXiv(self, feeds: list) -> list:
        """normalized arXiv feeds"""

        normalized = []
        for feed in feeds:
            entries = feed.get("entries", [])
            feed_dt = self._feed_updated_iso(feed)

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
                    "updated": self._entry_updated_iso(entry, feed_dt),
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
        if value.lower().startswith("arxiv.org/"):
            value = value[len("arxiv.org/"):]
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
