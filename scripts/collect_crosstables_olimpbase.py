#!/usr/bin/env python3
"""
Conservative OlimpBase HTML crosstable collector.

Start page:
    https://www.olimpbase.org/index.php

Purpose:
    Collect likely event/crosstable HTML pages for later conversion into
    your own event XML database.

Design:
    - No third-party packages.
    - Single-threaded only.
    - Long default delay: 8 seconds + jitter.
    - Honors robots.txt unless --no-robots is passed.
    - Saves original HTML bytes exactly as received.
    - Does not download images, ZIPs, PGNs, CSS, JS, etc.
    - Does not do a blind site crawl.
    - Walks:
        main index -> summary/index pages -> event landing pages -> local detail pages
    - Writes:
        manifest.csv
        summary_urls.txt
        candidate_event_urls.txt
        crosstable_urls.txt
        errors.txt

Recommended first run:
    py .\\olimpbase_collect_crosstables.py --out D:\\chess\\olimpbase --section individual --discover-only

Then inspect:
    D:\\chess\\olimpbase\\candidate_event_urls.txt

Then run:
    py .\\olimpbase_collect_crosstables.py --out D:\\chess\\olimpbase --section individual
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import (
    quote,
    unquote,
    urljoin,
    urlparse,
    urlunparse,
)
from urllib.request import Request, urlopen


START_URL = "https://www.olimpbase.org/index.php"
DEFAULT_OUT = r"D:\ctml\crosstables\raw\olimpbase"

BAD_EXTENSIONS = {
    ".gif", ".jpg", ".jpeg", ".png", ".svg", ".webp", ".ico",
    ".css", ".js",
    ".zip", ".pgn", ".cbv", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".txt",
}

BAD_LINK_WORDS = {
    "",
    "<<", ">>",
    "back", "home", "overview",
    "summary", "competition summary", "statistics", "missing data",
    "information", "books", "trivia",
    "search", "search for a player", "search for a team/club",
    "take a quiz", "vote in a poll!", "sign guestbook", "join newsletter",
    "contact us", "copyright policy", "credits", "author's foreword",
    "what's new", "literature", "writings' corner",
    "history", "today", "travel", "business",
}

SUMMARY_BAD_PATH_BITS = {
    "/img/",
    "/images/",
    "/help",
    "/search",
    "/guest",
    "/poll",
    "/quiz",
    "/contact",
    "/credits",
    "/copyright",
    "/newsletter",
}

EVENT_BAD_PATH_BITS = {
    "/img/",
    "/images/",
    "/help",
    "/search",
    "/guest",
    "/poll",
    "/quiz",
    "/contact",
    "/credits",
    "/copyright",
    "/newsletter",
}

DETAIL_LINK_WORDS = {
    "standings",
    "final standings",
    "crosstable",
    "the final group",
    "final group",
    "final",
    "preliminary",
    "preliminaries",
    "qualification",
    "qualifying",
    "group a",
    "group b",
    "group c",
    "group d",
    "group e",
    "group f",
    "stage",
}

CROSSTABLE_TEXT_MARKERS = {
    "final standings",
    "## final standings",
    "## standings",
    "crosstable",
    "## the final group",
    "stage:",
    "round:",
}

WINDOWS_BAD_CHARS = r'<>:"/\\|?*'


@dataclass(frozen=True)
class Link:
    url: str
    text: str


@dataclass
class FetchResult:
    url: str
    local_path: Path
    raw: bytes
    status: str


class LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[Link] = []

        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        attrs_dict = dict(attrs)

        if tag == "a":
            href = attrs_dict.get("href")
            if href:
                self._active_href = href
                self._active_text = []

        elif tag == "img" and self._active_href is not None:
            # Many OlimpBase links are image icons. Keep alt/title text
            # only while inside an anchor.
            alt = attrs_dict.get("alt") or attrs_dict.get("title")
            if alt:
                self._active_text.append(alt)

    def handle_data(self, data: str):
        if self._active_href is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() != "a":
            return

        if self._active_href is not None:
            full_url = urljoin(self.base_url, self._active_href)
            text = normalize_space("".join(self._active_text))
            self.links.append(Link(full_url, html.unescape(text)))

        self._active_href = None
        self._active_text = []


class Fetcher:
    def __init__(
        self,
        delay: float,
        jitter: float,
        timeout: float,
        retries: int,
        user_agent: str,
        max_fetches: int,
    ):
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.max_fetches = max_fetches

        self.last_fetch_started = 0.0
        self.fetch_count = 0

    def fetch(self, url: str) -> bytes:
        if self.fetch_count >= self.max_fetches:
            raise RuntimeError(
                f"Fetch limit reached: {self.max_fetches}. "
                f"Raise --max-fetches deliberately if needed."
            )

        for attempt in range(1, self.retries + 2):
            self._wait_before_fetch()

            req = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.8",
                },
            )

            self.fetch_count += 1

            try:
                with urlopen(req, timeout=self.timeout) as response:
                    return response.read()

            except HTTPError as e:
                retryable = e.code in {408, 429, 500, 502, 503, 504}
                if attempt <= self.retries and retryable:
                    wait = self._retry_wait(attempt, e.headers.get("Retry-After"))
                    print(
                        f"HTTP {e.code}; retrying in {wait:.1f}s: {url}",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise

            except URLError:
                if attempt <= self.retries:
                    wait = self._retry_wait(attempt, None)
                    print(
                        f"Network error; retrying in {wait:.1f}s: {url}",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError(f"Unreachable fetch failure: {url}")

    def _wait_before_fetch(self) -> None:
        now = time.time()
        elapsed = now - self.last_fetch_started

        target_delay = self.delay
        if self.jitter > 0:
            target_delay += random.uniform(0, self.jitter)

        if elapsed < target_delay:
            time.sleep(target_delay - elapsed)

        self.last_fetch_started = time.time()

    @staticmethod
    def _retry_wait(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(5.0, float(retry_after))
            except ValueError:
                pass

        return min(180.0, 10.0 * attempt * attempt)


def normalize_space(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def decode_for_parsing(raw: bytes) -> str:
    # OlimpBase has old pages and mixed European names. We save bytes exactly,
    # but need a forgiving decode only for link discovery and text heuristics.
    encodings = (
        "utf-8-sig",
        "utf-8",
        "windows-1250",
        "iso-8859-2",
        "windows-1252",
        "iso-8859-1",
    )

    for enc in encodings:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    return raw.decode("windows-1252", errors="replace")


def parse_links(raw: bytes, base_url: str) -> list[Link]:
    parser = LinkParser(base_url)
    parser.feed(decode_for_parsing(raw))
    return parser.links


def html_to_rough_text(raw: bytes) -> str:
    text = decode_for_parsing(raw)
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return normalize_space(text)


def canonical_url(url: str, start_url: str = START_URL) -> str:
    base_host = urlparse(start_url).netloc.lower()
    parsed = urlparse(url)

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()

    # Normalize olimpbase.org and www.olimpbase.org into the same host.
    if netloc in {"olimpbase.org", "www.olimpbase.org"}:
        netloc = base_host
        scheme = "https"

    # Remove fragments. Keep queries, though they are uncommon here.
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def same_site(url: str, start_url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    start_host = urlparse(start_url).netloc.lower().removeprefix("www.")
    return host == start_host


def path_suffix(url: str) -> str:
    return Path(urlparse(url).path.lower()).suffix


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme.lower() in {"http", "https"}


def is_htmlish_url(url: str) -> bool:
    suffix = path_suffix(url)
    return suffix in {"", ".html", ".htm", ".php"}


def is_asset_or_download(url: str) -> bool:
    return path_suffix(url) in BAD_EXTENSIONS


def link_text_key(text: str) -> str:
    return normalize_space(text).lower()


def is_code_or_score_text(text: str) -> bool:
    t = normalize_space(text)

    if not t:
        return True

    # Country/team abbreviations and scores create many non-event links.
    if re.fullmatch(r"[A-Z]{2,5}\d?", t):
        return True

    if re.fullmatch(r"\d+([½./:-]\d+)?", t):
        return True

    if re.fullmatch(r"[WDL]", t):
        return True

    if re.fullmatch(r"[+=-]+", t):
        return True

    return False


def has_bad_path_bit(url: str, bad_bits: set[str]) -> bool:
    path = urlparse(url).path.lower()
    return any(bit in path for bit in bad_bits)


def section_matches(url: str, section: str) -> bool:
    path = urlparse(url).path.lower()

    if section == "all":
        return True

    is_individual = "/ind-" in path

    if section == "individual":
        return is_individual

    if section == "team":
        return not is_individual

    raise ValueError(f"Unknown section: {section}")


def is_summary_seed_link(link: Link, start_url: str, section: str) -> bool:
    url = canonical_url(link.url, start_url)
    text = link_text_key(link.text)

    if not is_http_url(url):
        return False

    if not same_site(url, start_url):
        return False

    if is_asset_or_download(url) or not is_htmlish_url(url):
        return False

    if has_bad_path_bit(url, SUMMARY_BAD_PATH_BITS):
        return False

    if text in BAD_LINK_WORDS:
        return False

    if is_code_or_score_text(link.text):
        return False

    path = urlparse(url).path.lower()

    if path in {"/", "/index.php"}:
        return False

    if not section_matches(url, section):
        return False

    return True


def has_yearish_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    name = Path(path).name

    if re.search(r"(18|19|20)\d{2}", name):
        return True

    if re.search(r"/(18|19|20)\d{2}/", path):
        return True

    return False


def is_event_candidate_link(link: Link, parent_url: str, start_url: str, section: str) -> bool:
    url = canonical_url(link.url, start_url)
    parent = canonical_url(parent_url, start_url)
    text = link_text_key(link.text)

    if url == parent:
        return False

    if not is_http_url(url) or not same_site(url, start_url):
        return False

    if is_asset_or_download(url) or not is_htmlish_url(url):
        return False

    if has_bad_path_bit(url, EVENT_BAD_PATH_BITS):
        return False

    if text in BAD_LINK_WORDS:
        return False

    if is_code_or_score_text(link.text):
        return False

    if not section_matches(url, section):
        return False

    # Event pages nearly always have a year in the filename or directory:
    #   /ind-gbr/hastings-2024.html
    #   /ind-varia/it-london1851.html
    #   /2024/2024in.html
    if not has_yearish_url(url):
        return False

    return True


def same_directory(url_a: str, url_b: str) -> bool:
    pa = urlparse(url_a).path
    pb = urlparse(url_b).path
    return str(Path(pa).parent).lower() == str(Path(pb).parent).lower()


def is_round_link_text(text: str) -> bool:
    t = link_text_key(text)
    return bool(re.fullmatch(r"\d{1,2}(st|nd|rd|th)", t))


def is_detail_link(
    link: Link,
    parent_url: str,
    start_url: str,
    include_round_pages: bool,
) -> bool:
    url = canonical_url(link.url, start_url)
    parent = canonical_url(parent_url, start_url)
    text = link_text_key(link.text)

    if url == parent:
        return False

    if not same_site(url, start_url):
        return False

    if is_asset_or_download(url) or not is_htmlish_url(url):
        return False

    if not same_directory(url, parent):
        return False

    if has_bad_path_bit(url, EVENT_BAD_PATH_BITS):
        return False

    if text in {"summary", "competition summary", "statistics", "information", "books", "missing data"}:
        return False

    if text in DETAIL_LINK_WORDS:
        return True

    if include_round_pages and is_round_link_text(text):
        return True

    return False


def looks_like_crosstable_page(raw: bytes) -> bool:
    text = html_to_rough_text(raw).lower()

    for marker in CROSSTABLE_TEXT_MARKERS:
        if marker in text:
            return True

    # Fallback for older pages whose headings are sparse.
    has_table_vocabulary = (
        "pos." in text
        and "pts" in text
        and ("games" in text or "gms" in text)
    )

    has_chess_results = (
        " 1 " in text
        or " 0 " in text
        or " ½ " in text
        or "1/2" in text
    )

    return has_table_vocabulary and has_chess_results


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sanitize_segment(segment: str) -> str:
    segment = unquote(segment)
    segment = segment.strip()

    if not segment:
        return "_"

    for ch in WINDOWS_BAD_CHARS:
        segment = segment.replace(ch, "_")

    # Avoid Windows device names.
    lower = segment.lower()
    device_names = {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
    if lower in device_names:
        segment = f"_{segment}"

    return segment


def local_path_for_url(out_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    host = sanitize_segment(parsed.netloc.lower())

    parts = [sanitize_segment(p) for p in parsed.path.split("/") if p]

    if not parts:
        parts = ["index.html"]

    last = parts[-1]
    if "." not in last:
        parts[-1] = f"{last}.html"

    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        stem = Path(parts[-1]).stem
        suffix = Path(parts[-1]).suffix or ".html"
        parts[-1] = f"{stem}-{digest}{suffix}"

    return out_dir / "html" / host / Path(*parts)


def write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def collect_unique_links(links: Iterable[Link], start_url: str) -> list[Link]:
    seen: set[str] = set()
    out: list[Link] = []

    for link in links:
        url = canonical_url(link.url, start_url)
        if url not in seen:
            seen.add(url)
            out.append(Link(url, normalize_space(link.text)))

    return out


def write_url_file(path: Path, links: Iterable[Link]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [link.url for link in links]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "status",
        "role",
        "is_crosstable",
        "url",
        "parent_url",
        "link_text",
        "local_path",
        "bytes",
        "sha256",
        "error",
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_robots(start_url: str, user_agent: str, no_robots: bool, fail_closed: bool):
    if no_robots:
        print("robots.txt check disabled by --no-robots")
        return None

    robots_url = urljoin(start_url, "/robots.txt")
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)

    try:
        print(f"Reading robots.txt: {robots_url}")
        rp.read()
        return rp
    except Exception as e:
        msg = f"Could not read robots.txt: {type(e).__name__}: {e}"
        if fail_closed:
            raise RuntimeError(msg)
        print(f"Warning: {msg}", file=sys.stderr)
        return None


def robot_allowed(rp, user_agent: str, url: str) -> bool:
    if rp is None:
        return True
    return bool(rp.can_fetch(user_agent, url))


def fetch_or_cache(
    url: str,
    role: str,
    out_dir: Path,
    fetcher: Fetcher,
    rp,
    user_agent: str,
    refresh: bool,
) -> FetchResult:
    local_path = local_path_for_url(out_dir, url)

    if local_path.exists() and not refresh:
        return FetchResult(
            url=url,
            local_path=local_path,
            raw=local_path.read_bytes(),
            status="cached",
        )

    if not robot_allowed(rp, user_agent, url):
        raise PermissionError(f"Blocked by robots.txt: {url}")

    print(f"Fetching [{role}]: {url}")
    raw = fetcher.fetch(url)
    write_bytes(local_path, raw)

    return FetchResult(
        url=url,
        local_path=local_path,
        raw=raw,
        status="ok",
    )


def make_row(
    status: str,
    role: str,
    is_crosstable: bool,
    url: str,
    parent_url: str,
    link_text: str,
    local_path: Path | str,
    raw: bytes | None,
    error: str = "",
) -> dict[str, str]:
    return {
        "status": status,
        "role": role,
        "is_crosstable": "yes" if is_crosstable else "no",
        "url": url,
        "parent_url": parent_url,
        "link_text": link_text,
        "local_path": str(local_path) if local_path else "",
        "bytes": str(len(raw)) if raw is not None else "",
        "sha256": sha256_bytes(raw) if raw is not None else "",
        "error": error,
    }


def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--out", default=DEFAULT_OUT)

    ap.add_argument(
        "--section",
        choices=["all", "individual", "team"],
        default="all",
        help=(
            "all = collect from all internal index links; "
            "individual = mostly /ind-* tournament pages; "
            "team = mostly team-event pages."
        ),
    )

    ap.add_argument(
        "--discover-only",
        action="store_true",
        help="Fetch only the main index and summary pages, then write candidate_event_urls.txt.",
    )

    ap.add_argument(
        "--include-round-pages",
        action="store_true",
        help="Also collect 1st/2nd/3rd/etc. round detail pages within event directories.",
    )

    ap.add_argument("--delay", type=float, default=8.0, help="Base seconds between requests.")
    ap.add_argument("--jitter", type=float, default=2.0, help="Extra random seconds added to delay.")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--retries", type=int, default=3)

    ap.add_argument("--max-summary-pages", type=int, default=300)
    ap.add_argument("--max-event-pages", type=int, default=3000)
    ap.add_argument("--max-detail-pages", type=int, default=6000)
    ap.add_argument("--max-fetches", type=int, default=10000)

    ap.add_argument("--refresh", action="store_true", help="Re-fetch pages even if already cached.")
    ap.add_argument("--no-robots", action="store_true", help="Do not check robots.txt.")
    ap.add_argument(
        "--fail-closed-robots",
        action="store_true",
        help="Abort if robots.txt cannot be read.",
    )

    ap.add_argument(
        "--user-agent",
        default=(
            "OlimpBaseCrosstableCollector/1.0 "
            "(single-user archival chess research; polite single-threaded crawler)"
        ),
    )

    args = ap.parse_args()

    out_dir = Path(args.out)
    manifest_path = out_dir / "manifest.csv"
    errors_path = out_dir / "errors.txt"
    summary_urls_path = out_dir / "summary_urls.txt"
    candidate_urls_path = out_dir / "candidate_event_urls.txt"
    crosstable_urls_path = out_dir / "crosstable_urls.txt"

    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    errors: list[str] = []

    fetcher = Fetcher(
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        user_agent=args.user_agent,
        max_fetches=args.max_fetches,
    )

    rp = load_robots(
        start_url=args.start_url,
        user_agent=args.user_agent,
        no_robots=args.no_robots,
        fail_closed=args.fail_closed_robots,
    )

    start_url = canonical_url(args.start_url, args.start_url)

    try:
        index_result = fetch_or_cache(
            url=start_url,
            role="main_index",
            out_dir=out_dir,
            fetcher=fetcher,
            rp=rp,
            user_agent=args.user_agent,
            refresh=args.refresh,
        )

        rows.append(
            make_row(
                status=index_result.status,
                role="main_index",
                is_crosstable=False,
                url=start_url,
                parent_url="",
                link_text="",
                local_path=index_result.local_path,
                raw=index_result.raw,
            )
        )

    except Exception as e:
        msg = f"MAIN INDEX ERROR\t{start_url}\t{type(e).__name__}: {e}"
        errors.append(msg)
        print(msg, file=sys.stderr)
        errors_path.write_text("\n".join(errors) + "\n", encoding="utf-8")
        write_manifest(manifest_path, rows)
        return 1

    # Main index -> summary pages.
    index_links = parse_links(index_result.raw, start_url)

    summary_links = collect_unique_links(
        (
            Link(canonical_url(link.url, start_url), link.text)
            for link in index_links
            if is_summary_seed_link(link, start_url, args.section)
        ),
        start_url,
    )

    if len(summary_links) > args.max_summary_pages:
        print(
            f"Summary page candidates capped from {len(summary_links)} "
            f"to {args.max_summary_pages}."
        )
        summary_links = summary_links[: args.max_summary_pages]

    write_url_file(summary_urls_path, summary_links)

    print(f"Summary/index pages found: {len(summary_links)}")

    candidate_event_links: list[Link] = []

    for i, summary_link in enumerate(summary_links, start=1):
        try:
            result = fetch_or_cache(
                url=summary_link.url,
                role="summary_page",
                out_dir=out_dir,
                fetcher=fetcher,
                rp=rp,
                user_agent=args.user_agent,
                refresh=args.refresh,
            )

            rows.append(
                make_row(
                    status=result.status,
                    role="summary_page",
                    is_crosstable=False,
                    url=summary_link.url,
                    parent_url=start_url,
                    link_text=summary_link.text,
                    local_path=result.local_path,
                    raw=result.raw,
                )
            )

            links = parse_links(result.raw, summary_link.url)
            event_links = [
                Link(canonical_url(link.url, start_url), link.text)
                for link in links
                if is_event_candidate_link(
                    link=link,
                    parent_url=summary_link.url,
                    start_url=start_url,
                    section=args.section,
                )
            ]

            candidate_event_links.extend(event_links)

            print(
                f"[{i}/{len(summary_links)}] "
                f"{summary_link.text or summary_link.url}: "
                f"{len(event_links)} event candidates"
            )

        except Exception as e:
            msg = f"SUMMARY ERROR\t{summary_link.url}\t{type(e).__name__}: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

            rows.append(
                make_row(
                    status="error",
                    role="summary_page",
                    is_crosstable=False,
                    url=summary_link.url,
                    parent_url=start_url,
                    link_text=summary_link.text,
                    local_path="",
                    raw=None,
                    error=f"{type(e).__name__}: {e}",
                )
            )

    candidate_event_links = collect_unique_links(candidate_event_links, start_url)

    if len(candidate_event_links) > args.max_event_pages:
        print(
            f"Event page candidates capped from {len(candidate_event_links)} "
            f"to {args.max_event_pages}."
        )
        candidate_event_links = candidate_event_links[: args.max_event_pages]

    write_url_file(candidate_urls_path, candidate_event_links)

    print(f"Unique event candidates: {len(candidate_event_links)}")

    if args.discover_only:
        write_manifest(manifest_path, rows)
        errors_path.write_text(
            "\n".join(errors) + ("\n" if errors else ""),
            encoding="utf-8",
        )

        print()
        print("Discovery complete.")
        print(f"Summary URLs:    {summary_urls_path.resolve()}")
        print(f"Candidate URLs:  {candidate_urls_path.resolve()}")
        print(f"Manifest:        {manifest_path.resolve()}")
        print(f"Errors:          {errors_path.resolve()}")
        return 0 if not errors else 1

    # Event candidates -> crosstable pages and local detail pages.
    crosstable_links: list[Link] = []
    detail_queue: list[tuple[str, Link]] = []
    queued_detail_urls: set[str] = set()

    for i, event_link in enumerate(candidate_event_links, start=1):
        try:
            result = fetch_or_cache(
                url=event_link.url,
                role="event_page",
                out_dir=out_dir,
                fetcher=fetcher,
                rp=rp,
                user_agent=args.user_agent,
                refresh=args.refresh,
            )

            is_cross = looks_like_crosstable_page(result.raw)

            if is_cross:
                crosstable_links.append(event_link)

            rows.append(
                make_row(
                    status=result.status,
                    role="event_page",
                    is_crosstable=is_cross,
                    url=event_link.url,
                    parent_url="",
                    link_text=event_link.text,
                    local_path=result.local_path,
                    raw=result.raw,
                )
            )

            print(
                f"[{i}/{len(candidate_event_links)}] "
                f"{'CROSSTABLE' if is_cross else 'landing'}: "
                f"{event_link.text or event_link.url}"
            )

            links = parse_links(result.raw, event_link.url)
            for link in links:
                if is_detail_link(
                    link=link,
                    parent_url=event_link.url,
                    start_url=start_url,
                    include_round_pages=args.include_round_pages,
                ):
                    detail_url = canonical_url(link.url, start_url)
                    if detail_url not in queued_detail_urls:
                        queued_detail_urls.add(detail_url)
                        detail_queue.append(
                            (event_link.url, Link(detail_url, link.text))
                        )

        except Exception as e:
            msg = f"EVENT ERROR\t{event_link.url}\t{type(e).__name__}: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

            rows.append(
                make_row(
                    status="error",
                    role="event_page",
                    is_crosstable=False,
                    url=event_link.url,
                    parent_url="",
                    link_text=event_link.text,
                    local_path="",
                    raw=None,
                    error=f"{type(e).__name__}: {e}",
                )
            )

    if len(detail_queue) > args.max_detail_pages:
        print(
            f"Detail page queue capped from {len(detail_queue)} "
            f"to {args.max_detail_pages}."
        )
        detail_queue = detail_queue[: args.max_detail_pages]

    # Process detail pages. While doing so, allow a small same-directory
    # expansion for pages named "Crosstable", "Standings", etc.
    processed_detail_urls: set[str] = set()
    detail_index = 0

    while detail_index < len(detail_queue):
        parent_url, detail_link = detail_queue[detail_index]
        detail_index += 1

        if detail_link.url in processed_detail_urls:
            continue

        if len(processed_detail_urls) >= args.max_detail_pages:
            print(f"Detail page limit reached: {args.max_detail_pages}")
            break

        processed_detail_urls.add(detail_link.url)

        try:
            result = fetch_or_cache(
                url=detail_link.url,
                role="detail_page",
                out_dir=out_dir,
                fetcher=fetcher,
                rp=rp,
                user_agent=args.user_agent,
                refresh=args.refresh,
            )

            is_cross = looks_like_crosstable_page(result.raw)

            if is_cross:
                crosstable_links.append(detail_link)

            rows.append(
                make_row(
                    status=result.status,
                    role="detail_page",
                    is_crosstable=is_cross,
                    url=detail_link.url,
                    parent_url=parent_url,
                    link_text=detail_link.text,
                    local_path=result.local_path,
                    raw=result.raw,
                )
            )

            print(
                f"[detail {len(processed_detail_urls)}/{len(detail_queue)}] "
                f"{'CROSSTABLE' if is_cross else 'detail'}: "
                f"{detail_link.text or detail_link.url}"
            )

            # Limited local expansion: from a standings page, pick up the
            # crosstable page; optionally pick up round pages.
            links = parse_links(result.raw, detail_link.url)
            for link in links:
                if is_detail_link(
                    link=link,
                    parent_url=detail_link.url,
                    start_url=start_url,
                    include_round_pages=args.include_round_pages,
                ):
                    child_url = canonical_url(link.url, start_url)
                    if (
                        child_url not in queued_detail_urls
                        and child_url not in processed_detail_urls
                        and len(detail_queue) < args.max_detail_pages
                    ):
                        queued_detail_urls.add(child_url)
                        detail_queue.append(
                            (detail_link.url, Link(child_url, link.text))
                        )

        except Exception as e:
            msg = f"DETAIL ERROR\t{detail_link.url}\t{type(e).__name__}: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

            rows.append(
                make_row(
                    status="error",
                    role="detail_page",
                    is_crosstable=False,
                    url=detail_link.url,
                    parent_url=parent_url,
                    link_text=detail_link.text,
                    local_path="",
                    raw=None,
                    error=f"{type(e).__name__}: {e}",
                )
            )

    crosstable_links = collect_unique_links(crosstable_links, start_url)
    write_url_file(crosstable_urls_path, crosstable_links)

    write_manifest(manifest_path, rows)
    errors_path.write_text(
        "\n".join(errors) + ("\n" if errors else ""),
        encoding="utf-8",
    )

    ok_count = sum(1 for row in rows if row["status"] in {"ok", "cached"})
    error_count = sum(1 for row in rows if row["status"] == "error")
    crosstable_count = sum(1 for row in rows if row["is_crosstable"] == "yes")

    print()
    print("Done.")
    print(f"Fetched/cached pages: {ok_count}")
    print(f"Crosstable-like pages: {crosstable_count}")
    print(f"Errors: {error_count}")
    print(f"HTTP fetches this run: {fetcher.fetch_count}")
    print(f"Output folder: {out_dir.resolve()}")
    print(f"Manifest: {manifest_path.resolve()}")
    print(f"Crosstable URLs: {crosstable_urls_path.resolve()}")
    print(f"Errors log: {errors_path.resolve()}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())