#!/usr/bin/env python3
"""
Download Northwest Chess / Minev tournament crosstable HTML files.

Start page:
    https://nwchess.com/minev/crosstableindex.html

What it does:
    1. Downloads the main crosstable index.
    2. Finds the era/list pages, e.g. Events 1900-1919.
    3. Finds individual crosstable HTML links under /minev/files/.
    4. Downloads those original HTML files as raw bytes.
    5. Writes:
        - manifest.csv
        - crosstable_urls.txt
        - errors.txt

No third-party packages required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import os
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen


START_URL = "https://nwchess.com/minev/crosstableindex.html"
DEFAULT_OUT = r"D:\ctml\crosstables\raw\nwchess-minev"


@dataclass
class Link:
    url: str
    text: str


class AnchorParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[Link] = []
        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return

        attr = dict(attrs)
        href = attr.get("href")
        if href:
            self._active_href = href
            self._active_text = []

    def handle_data(self, data: str):
        if self._active_href is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() != "a":
            return

        if self._active_href is not None:
            full_url = urljoin(self.base_url, self._active_href)
            text = " ".join("".join(self._active_text).split())
            self.links.append(Link(full_url, html.unescape(text)))

        self._active_href = None
        self._active_text = []


class Fetcher:
    def __init__(self, delay: float, timeout: float, retries: int):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.last_fetch = 0.0

    def fetch(self, url: str) -> bytes:
        for attempt in range(1, self.retries + 2):
            self._respect_delay()

            req = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 compatible; "
                        "CrosstableArchiver/1.0; respectful historical chess research"
                    )
                },
            )

            try:
                with urlopen(req, timeout=self.timeout) as response:
                    return response.read()

            except HTTPError as e:
                retry_after = e.headers.get("Retry-After")
                should_retry = e.code in {429, 500, 502, 503, 504}

                if attempt <= self.retries and should_retry:
                    wait = self._retry_wait(attempt, retry_after)
                    print(f"HTTP {e.code}; retrying in {wait:.1f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue

                raise

            except URLError:
                if attempt <= self.retries:
                    wait = self._retry_wait(attempt, None)
                    print(f"Network error; retrying in {wait:.1f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue

                raise

        raise RuntimeError(f"Unreachable fetch failure: {url}")

    def _respect_delay(self) -> None:
        elapsed = time.time() - self.last_fetch
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_fetch = time.time()

    @staticmethod
    def _retry_wait(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        return min(60.0, 2.0 ** attempt)


def decode_for_parsing(raw: bytes) -> str:
    for encoding in ("utf-8", "windows-1252", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("windows-1252", errors="replace")


def parse_links(raw: bytes, base_url: str) -> list[Link]:
    parser = AnchorParser(base_url)
    parser.feed(decode_for_parsing(raw))
    return parser.links


def is_same_site(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "nwchess.com"


def is_html_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".html") or path.endswith(".htm")


def is_era_page(link: Link) -> bool:
    if not is_same_site(link.url) or not is_html_url(link.url):
        return False

    basename = Path(urlparse(link.url).path).name.lower()
    text = link.text.lower()

    return basename.startswith("events") or text.startswith("events ")


def is_crosstable_file(link: Link) -> bool:
    if not is_same_site(link.url) or not is_html_url(link.url):
        return False

    path = urlparse(link.url).path.lower()
    return "/minev/files/" in path


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)

    # Windows-safe filename cleanup.
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    if not name:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        name = f"crosstable-{digest}.html"

    return name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    for i in range(2, 10_000):
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find unique filename for {path}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def collect_unique_links(links: Iterable[Link]) -> list[Link]:
    seen: set[str] = set()
    result: list[Link] = []

    for link in links:
        normalized = link.url.split("#", 1)[0]
        if normalized not in seen:
            seen.add(normalized)
            result.append(Link(normalized, link.text))

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between requests.")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    index_dir = out_dir / "_index_pages"
    files_dir = out_dir / "files"
    manifest_path = out_dir / "manifest.csv"
    urls_path = out_dir / "crosstable_urls.txt"
    errors_path = out_dir / "errors.txt"

    out_dir.mkdir(parents=True, exist_ok=True)

    fetcher = Fetcher(delay=args.delay, timeout=args.timeout, retries=args.retries)
    errors: list[str] = []

    print(f"Fetching main index: {args.start_url}")
    index_raw = fetcher.fetch(args.start_url)
    write_bytes(index_dir / "crosstableindex.html", index_raw)

    index_links = parse_links(index_raw, args.start_url)
    era_pages = collect_unique_links(link for link in index_links if is_era_page(link))

    print(f"Found {len(era_pages)} era/list pages.")

    crosstable_links: list[tuple[Link, Link]] = []

    for era in era_pages:
        print(f"Fetching era page: {era.text or era.url}")

        try:
            era_raw = fetcher.fetch(era.url)
            era_name = safe_filename_from_url(era.url)
            write_bytes(index_dir / era_name, era_raw)

            links = parse_links(era_raw, era.url)
            for link in links:
                if is_crosstable_file(link):
                    crosstable_links.append((era, link))

        except Exception as e:
            msg = f"ERA PAGE ERROR\t{era.url}\t{type(e).__name__}: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

    # Deduplicate by crosstable URL, preserving first era/title encountered.
    seen_urls: set[str] = set()
    unique_crosstables: list[tuple[Link, Link]] = []

    for era, link in crosstable_links:
        clean_url = link.url.split("#", 1)[0]
        if clean_url not in seen_urls:
            seen_urls.add(clean_url)
            unique_crosstables.append((era, Link(clean_url, link.text)))

    print(f"Found {len(unique_crosstables)} unique crosstable HTML files.")

    urls_path.write_text(
        "\n".join(link.url for _, link in unique_crosstables) + "\n",
        encoding="utf-8",
    )

    if args.dry_run:
        print("Dry run only. URLs written, but crosstable files were not downloaded.")
        return 0

    rows: list[dict[str, str]] = []

    for i, (era, link) in enumerate(unique_crosstables, start=1):
        print(f"[{i}/{len(unique_crosstables)}] Downloading: {link.text or link.url}")

        try:
            raw = fetcher.fetch(link.url)

            filename = safe_filename_from_url(link.url)
            local_path = unique_path(files_dir / filename)
            write_bytes(local_path, raw)

            rows.append(
                {
                    "status": "ok",
                    "title": link.text,
                    "era": era.text,
                    "url": link.url,
                    "local_path": str(local_path),
                    "bytes": str(len(raw)),
                    "sha256": sha256_bytes(raw),
                    "error": "",
                }
            )

        except Exception as e:
            msg = f"CROSSTABLE ERROR\t{link.url}\t{type(e).__name__}: {e}"
            errors.append(msg)
            print(msg, file=sys.stderr)

            rows.append(
                {
                    "status": "error",
                    "title": link.text,
                    "era": era.text,
                    "url": link.url,
                    "local_path": "",
                    "bytes": "",
                    "sha256": "",
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "status",
            "title",
            "era",
            "url",
            "local_path",
            "bytes",
            "sha256",
            "error",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    errors_path.write_text("\n".join(errors) + ("\n" if errors else ""), encoding="utf-8")

    ok_count = sum(1 for row in rows if row["status"] == "ok")
    error_count = sum(1 for row in rows if row["status"] == "error")

    print()
    print("Done.")
    print(f"Downloaded: {ok_count}")
    print(f"Errors:     {error_count}")
    print(f"Output:     {out_dir.resolve()}")
    print(f"Manifest:   {manifest_path.resolve()}")
    print(f"URL list:   {urls_path.resolve()}")
    print(f"Errors log: {errors_path.resolve()}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())