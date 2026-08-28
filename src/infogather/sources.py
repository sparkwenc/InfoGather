import time
import http.client
import feedparser
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .filters import parse_updated


MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_SOURCE_SELECTOR_LENGTH = 256


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag == "mml:math":
            alt_text = dict(attrs).get("alttext")
            if alt_text:
                self.parts.append(f" {alt_text} ")
            self.skip_depth = 1
            return
        if tag in {"br", "div", "li", "p"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in {"div", "li", "p"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def _extract_arxiv_tag(url: str) -> str | None:
    path = urlparse(url).path.strip("/")
    if not path:
        return None
    return path.split("/")[-1].strip() or None


def configured_sources(conf: dict) -> list[dict]:
    sources = []
    seen_urls = set()
    seen_keys = set()
    for srce_ty, raw_sources in conf.items():
        if not isinstance(raw_sources, list):
            raise ValueError(f"source group {srce_ty!r} must be a list")
        for item in raw_sources:
            if not isinstance(item, dict):
                raise ValueError(f"source in {srce_ty!r} must be a table")
            name = str(item.get("name", "")).strip() or "unknown"
            url = str(item.get("url", "")).strip()
            if not url:
                raise ValueError(f"source {name!r} has no URL")
            if url in seen_urls:
                continue
            key = (
                _extract_arxiv_tag(url)
                if srce_ty == "arXiv"
                else str(item.get("key", "")).strip() or name
            )
            if not key:
                raise ValueError(f"source {name!r} has no key")
            identity = (str(srce_ty), key)
            if identity in seen_keys:
                raise ValueError(f"duplicate source key: {srce_ty}:{key}")
            selector = key if srce_ty == "arXiv" else f"source:{srce_ty}:{key}"
            if len(selector) > MAX_SOURCE_SELECTOR_LENGTH:
                raise ValueError(f"source selector is too long: {srce_ty}:{key}")
            seen_urls.add(url)
            seen_keys.add(identity)
            sources.append({
                "srce_ty": str(srce_ty),
                "key": key,
                "name": name,
                "url": url,
                "selector_value": selector,
            })
    return sources


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
        self.total_feeds = 0
        self.cached_feeds = 0
        self.failed_feeds = 0

    def get_normalized_feeds(self) -> list:
        """return normalized entries from all sources"""
        self.feed_state_updates = {}
        raw_feeds, state_updates = self._fetch_raw_feeds()

        print("Normalizing feeds...")
        arxiv = self._normalized_arXiv([
            item["feed"] for item in raw_feeds
            if item["source"]["srce_ty"] == "arXiv"
        ])
        journals = self._normalized_rss([
            item for item in raw_feeds
            if item["source"]["srce_ty"] != "arXiv"
        ])
        print(f"arXiv: {len(arxiv):3d} normalized")
        print(f"Journals: {len(journals):3d} normalized")
        normalized = [*arxiv, *journals]
        deduplicated = self._deduplicate_entries(normalized)
        duplicate_count = len(normalized) - len(deduplicated)
        if duplicate_count:
            print(f"Deduplicated: {duplicate_count:3d} cross-listed entries")
        print(f"Total: {len(deduplicated):3d} normalized\n")
        self.feed_state_updates = state_updates
        return deduplicated

    # internal methods
    def _fetch_raw_feeds(self) -> tuple[list, dict[str, dict]]:
        """fetch all feeds according to the configuration"""

        feeds: dict[int, dict] = {}
        state_updates: dict[str, dict] = {}
        requests = configured_sources(self._conf)

        total = len(requests)
        self.total_feeds = total
        print(
            f"Fetching {total} feeds with up to "
            f"{min(self._max_workers, max(total, 1))} concurrent requests..."
        )
        if not requests:
            return [], state_updates

        now = time.time()
        pending = []
        completed = 0
        cached = 0
        failed = 0
        total_entries = 0
        for index, source in enumerate(requests):
            name = source["name"]
            url = source["url"]
            state = self._feed_states.get(url, {})
            if float(state.get("next_fetch_at", 0) or 0) > now:
                completed += 1
                cached += 1
                print(f"SOURCE {completed}/{total}: cached {name}")
                continue
            pending.append((index, source, state))

        with ThreadPoolExecutor(
            max_workers=min(self._max_workers, max(len(pending), 1))
        ) as executor:
            futures = {
                executor.submit(
                    self._fetch_feed,
                    source["url"],
                    source["name"],
                    state,
                ): (index, source)
                for index, source, state in pending
            }
            for future in as_completed(futures):
                index, source = futures[future]
                name = source["name"]
                url = source["url"]
                completed += 1
                try:
                    feed, state_update = future.result()
                    state_updates[url] = state_update
                    if feed is None:
                        cached += 1
                        print(f"SOURCE {completed}/{total}: not modified {name}")
                        continue
                    count = len(feed.get("entries", []))
                    feeds[index] = {"source": source, "feed": feed}
                    total_entries += count
                    print(f"SOURCE {completed}/{total}: {count:3d} from {name}")
                except Exception as exc:
                    failed += 1
                    print(f"SOURCE {completed}/{total}: failed {name}: {exc}")

        print(
            f"Fetch result: {total_entries} entries, {cached} cached, "
            f"{failed} failed from {total} feeds\n"
        )
        self.cached_feeds = cached
        self.failed_feeds = failed
        if failed == total and not cached:
            raise RuntimeError("all configured feeds failed")
        return [feeds[index] for index in sorted(feeds)], state_updates

    @staticmethod
    def _fetch_feed(
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
                status_code, response_headers, response_content = (
                    InfoSources._read_feed_response(
                        url, headers=headers, name=name
                    )
                )
            except (http.client.HTTPException, OSError) as exc:
                if attempt >= attempts:
                    raise
                print(
                    f"      {name}: transport error ({exc}), "
                    f"retrying {attempt + 1}/{attempts}"
                )
                time.sleep(delay * (2 ** (attempt - 1)))
                continue

            state_update = InfoSources._feed_state_from_headers(
                response_headers,
                previous=state,
                status_code=status_code,
            )
            if status_code == 304:
                return None, state_update

            if status_code < 200 or status_code >= 300:
                error = RuntimeError(
                    f"{name}: feed request failed with HTTP {status_code}"
                )
                retryable = (
                    status_code in {408, 425, 429}
                    or status_code >= 500
                )
                if not retryable or attempt >= attempts:
                    raise error
                print(f"      {error}, retrying {attempt + 1}/{attempts}")
                retry_after = response_headers.get("retry-after", "")
                try:
                    wait = min(max(float(retry_after), 0), 60)
                except (TypeError, ValueError):
                    wait = delay * (2 ** (attempt - 1))
                time.sleep(wait)
                continue

            normalized_headers = {
                str(key).lower(): value
                for key, value in response_headers.items()
            }
            if "json" in str(normalized_headers.get("content-type", "")).lower():
                try:
                    feed = InfoSources._crossref_feed(
                        json.loads(response_content)
                    )
                except (TypeError, ValueError) as exc:
                    feed = {"bozo": True, "bozo_exception": exc, "entries": []}
            else:
                feed = feedparser.parse(
                    response_content,
                    response_headers=normalized_headers,
                )
            if feed.get("bozo"):
                detail = feed.get("bozo_exception", "parse error")
                if attempt < attempts:
                    print(
                        f"      {name}: invalid feed ({detail}), "
                        f"retrying {attempt + 1}/{attempts}"
                    )
                    time.sleep(delay * (2 ** (attempt - 1)))
                    continue
                raise RuntimeError(f"{name}: invalid feed: {detail}")
            return feed, state_update

        raise RuntimeError("unreachable feed fetch state")

    @staticmethod
    def _crossref_feed(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Crossref response must be an object")
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            raise ValueError("Crossref response has no works")

        entries = []
        for work in message["items"]:
            if not isinstance(work, dict):
                continue
            titles = work.get("title", [])
            if isinstance(titles, list):
                title = str(titles[0]).strip() if titles else ""
            else:
                title = str(titles or "").strip()
            if not title:
                continue
            authors = []
            for author in work.get("author", []) or []:
                if not isinstance(author, dict):
                    continue
                name = " ".join(
                    str(author.get(part, "")).strip()
                    for part in ("given", "family")
                    if str(author.get(part, "")).strip()
                )
                if name:
                    authors.append(name)
            resource = work.get("resource", {})
            if not isinstance(resource, dict):
                resource = {}
            primary = resource.get("primary", {})
            if not isinstance(primary, dict):
                primary = {}
            identifier = str(work.get("DOI") or "").strip()
            link = primary.get("URL") or work.get("URL")
            if not identifier and not link:
                continue
            entry = {
                "id": identifier,
                "link": link,
                "title": title,
                "author": ", ".join(authors),
                "summary": str(work.get("abstract") or ""),
            }
            published = work.get("published", {})
            if not isinstance(published, dict):
                published = {}
            date_parts = published.get("date-parts", [])
            if date_parts and isinstance(date_parts[0], list):
                try:
                    parts = [*date_parts[0], 1, 1][:3]
                    entry["published_parsed"] = datetime(*parts).timetuple()
                except (TypeError, ValueError, OverflowError):
                    pass
            entries.append(entry)
        return {"feed": {}, "entries": entries}

    @staticmethod
    def _read_feed_response(
        url: str,
        *,
        headers: dict[str, str],
        name: str,
    ) -> tuple[int, object, bytes]:
        request = Request(
            url,
            headers={
                "User-Agent": "InfoGather/0.1 (+local RSS reader)",
                **headers,
            },
        )
        try:
            response = urlopen(request, timeout=12)
        except HTTPError as exc:
            return exc.code, exc.headers, b""
        with response:
            response_headers = response.headers
            status_code = response.status
            InfoSources._validate_content_length(response_headers, name)
            content = response.read(MAX_FEED_BYTES + 1)
        if len(content) > MAX_FEED_BYTES:
            raise RuntimeError(f"{name}: feed response is too large")
        return status_code, response_headers, content

    @staticmethod
    def _validate_content_length(headers: object, name: str) -> None:
        raw_length = getattr(headers, "get")("content-length")
        if not raw_length:
            return
        try:
            too_large = int(raw_length) > MAX_FEED_BYTES
        except (TypeError, ValueError):
            return
        if too_large:
            raise RuntimeError(f"{name}: feed response is too large")

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
                get_header("etag")
                or (previous.get("etag") if status_code == 304 else None)
            ),
            "last_modified": None if no_store else (
                get_header("last-modified")
                or (
                    previous.get("last_modified")
                    if status_code == 304 else None
                )
            ),
            "next_fetch_at": time.time() + fresh_for,
        }

    @staticmethod
    def _deduplicate_entries(entries: list[dict]) -> list[dict]:
        unique: dict[tuple[str, str], dict] = {}
        for entry in entries:
            key = (entry["srce_ty"], entry["srce_id"])
            existing = unique.get(key)
            if existing is None:
                unique[key] = entry
                continue
            existing_tags = existing["content"].get("tags", [])
            incoming_tags = entry["content"].get("tags", [])
            merged_tags = sorted(set([*existing_tags, *incoming_tags]))
            incoming_updated = parse_updated(str(entry.get("updated", "")))
            existing_updated = parse_updated(str(existing.get("updated", "")))
            use_incoming = entry["version"] > existing["version"] or (
                entry["version"] == existing["version"]
                and incoming_updated is not None
                and (existing_updated is None or incoming_updated > existing_updated)
            )
            if use_incoming:
                entry["content"]["tags"] = merged_tags
                unique[key] = entry
            else:
                existing["content"]["tags"] = merged_tags
        return list(unique.values())

    @staticmethod
    def _parsed_iso(value: dict) -> str | None:
        for key in ("updated_parsed", "published_parsed"):
            candidate = value.get(key)
            if candidate is None:
                continue
            try:
                return datetime(
                    *time.struct_time(candidate)[:6], tzinfo=timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    def _normalized_arXiv(self, feeds: list) -> list:
        """normalized arXiv feeds"""

        normalized = []
        for feed in feeds:
            entries = feed.get("entries", [])
            feed_dt = self._parsed_iso(feed.get("feed", {}))
            feed_dt = feed_dt or datetime.now(timezone.utc).isoformat()

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
                    "updated": self._parsed_iso(entry) or feed_dt,
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
    def _plain_text(value: object) -> str:
        parser = _TextExtractor()
        parser.feed(str(value or ""))
        parser.close()
        return " ".join(unescape("".join(parser.parts)).split())

    def _normalized_rss(self, feeds: list[dict]) -> list[dict]:
        normalized = []
        for item in feeds:
            source = item["source"]
            feed = item["feed"]
            feed_dt = self._parsed_iso(feed.get("feed", {}))
            feed_dt = feed_dt or datetime.now(timezone.utc).isoformat()
            source_url = urlparse(source["url"])

            for entry in feed.get("entries", []):
                raw_id = entry.get("id") or entry.get("link")
                if not isinstance(raw_id, str) or not raw_id.strip():
                    print(f"{source['name']}: entry without ID or link skipped")
                    continue
                title = self._plain_text(entry.get("title"))

                author = str(entry.get("author") or "").strip()
                if not author:
                    author = ", ".join(
                        str(value.get("name") or "").strip()
                        for value in entry.get("authors", []) or []
                        if isinstance(value, dict) and value.get("name")
                    )

                summary = str(entry.get("summary") or "")
                if (
                    not author
                    and source_url.hostname in {"ams.org", "www.ams.org"}
                    and source_url.path.startswith("/rss/")
                ):
                    parts = re.split(r"<br\s*/?>", summary, maxsplit=1, flags=re.I)
                    if len(parts) == 2:
                        author = self._plain_text(parts[0])
                        summary = parts[1]
                if source_url.hostname == "annals.math.princeton.edu":
                    # The feed's dc:creator is a WordPress editor, not the paper author.
                    author = ""

                content = entry.get("content", []) or []
                if content and isinstance(content[0], dict):
                    summary = str(content[0].get("value") or summary)
                tags = []
                for tag in entry.get("tags", []) or []:
                    if not isinstance(tag, dict):
                        continue
                    term = tag.get("term")
                    if isinstance(term, str) and term.strip():
                        tags.append(term.strip())
                tags.append(source["selector_value"])

                normalized.append({
                    "srce_ty": source["srce_ty"],
                    "srce_id": f"{source['key']}:{raw_id.strip()}",
                    "version": 1,
                    "favored": 0,
                    "noticed": 0,
                    "updated": self._parsed_iso(entry) or feed_dt,
                    "content": {
                        "link": entry.get("link"),
                        "titl": title,
                        "auth": author,
                        "abst": self._plain_text(summary),
                        "source": source["name"],
                        "tags": list(dict.fromkeys(tags)),
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
