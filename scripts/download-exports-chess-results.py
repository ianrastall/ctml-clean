#!/usr/bin/env python3
"""
Conservative Chess-Results Excel-export downloader.

Input:
    chessresults_manifest.csv created from TournamentSearch.xlsx files.

Default behavior:
    - Downloads only the first 5 tournaments as a pilot.
    - Uses known DB-Key URLs, not the search form.
    - Uses Excel-export URLs:
        ?lan=1&zeilen=0&prt=4&excel=2010
        plus art=... where appropriate.
    - Single-threaded.
    - Long delay.
    - Resume-safe.
    - Saves raw downloaded bytes exactly as received.
    - Logs every attempt to download_manifest.csv.

Recommended first run:
    py .\chessresults_download_exports.py --manifest D:\chess\chess-results-june-2026-manifest\chessresults_manifest.csv --out D:\chess\chess-results-june-2026-exports --only-db 1452154 --delay 15

Then:
    py .\chessresults_download_exports.py --manifest D:\chess\chess-results-june-2026-manifest\chessresults_manifest.csv --out D:\chess\chess-results-june-2026-exports --limit 20 --delay 15
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_TYPES = [
    "starting_rank",
    "final_ranking",
    "pairings_results",
    "final_crosstable",
    "starting_crosstable",
    "statistics",
    "playing_schedule",
]

EXPORT_TYPES = {
    "starting_rank": {
        "art": None,
        "turdet": False,
        "description": "Starting rank / player list",
    },
    "final_ranking": {
        "art": "1",
        "turdet": True,
        "description": "Final ranking",
    },
    "pairings_results": {
        "art": "2",
        "turdet": True,
        "description": "Pairings/results",
    },
    "final_crosstable": {
        "art": "4",
        "turdet": True,
        "description": "Final ranking crosstable",
    },
    "starting_crosstable": {
        "art": "5",
        "turdet": True,
        "description": "Starting rank crosstable",
    },
    "statistics": {
        "art": "13",
        "turdet": True,
        "description": "Statistics",
    },
    "playing_schedule": {
        "art": "14",
        "turdet": True,
        "description": "Playing schedule",
    },
    "alphabetical_list": {
        "art": "3",
        "turdet": True,
        "description": "Alphabetical list",
    },
    "alphabetical_all_groups": {
        "art": "79",
        "turdet": True,
        "description": "Alphabetical list, all groups",
    },
}

USER_AGENT = (
    "ChessResultsCollector/1.0 "
    "(single-user chess event archival research; slow single-threaded fetcher)"
)


@dataclass
class Tournament:
    db_key: str
    title: str
    start_date: str
    end_date: str
    system: str
    source_file: str


class Fetcher:
    def __init__(
        self,
        delay: float,
        jitter: float,
        timeout: float,
        retries: int,
        user_agent: str,
    ):
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.last_fetch_started = 0.0
        self.fetch_count = 0

    def fetch(self, url: str) -> tuple[bytes, str, str]:
        for attempt in range(1, self.retries + 2):
            self._wait()

            req = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                        "application/vnd.ms-excel,"
                        "text/html,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.8",
                },
            )

            self.fetch_count += 1

            try:
                with urlopen(req, timeout=self.timeout) as response:
                    raw = response.read()
                    final_url = response.geturl()
                    content_type = response.headers.get("Content-Type", "")
                    return raw, final_url, content_type

            except HTTPError as e:
                retryable = e.code in {408, 429, 500, 502, 503, 504}
                if attempt <= self.retries and retryable:
                    wait = retry_wait(attempt, e.headers.get("Retry-After"))
                    print(f"HTTP {e.code}; retrying in {wait:.1f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise

            except URLError as e:
                if attempt <= self.retries:
                    wait = retry_wait(attempt, None)
                    print(f"Network error; retrying in {wait:.1f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError(f"Unreachable fetch failure: {url}")

    def _wait(self) -> None:
        elapsed = time.time() - self.last_fetch_started
        target = self.delay + (random.uniform(0, self.jitter) if self.jitter else 0)

        if elapsed < target:
            time.sleep(target - elapsed)

        self.last_fetch_started = time.time()


def retry_wait(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(10.0, float(retry_after))
        except ValueError:
            pass

    return min(300.0, 15.0 * attempt * attempt)


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def safe_filename_part(value: str, max_len: int = 80) -> str:
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ._")

    if not value:
        value = "untitled"

    return value[:max_len]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def detect_extension(raw: bytes, content_type: str) -> str:
    ct = content_type.lower()

    if raw.startswith(b"PK\x03\x04"):
        return ".xlsx"

    if "spreadsheetml" in ct:
        return ".xlsx"

    if "ms-excel" in ct:
        return ".xls"

    stripped = raw.lstrip()[:200].lower()
    if stripped.startswith(b"<!doctype") or stripped.startswith(b"<html") or b"<html" in stripped:
        return ".html"

    if stripped.startswith(b"%pdf"):
        return ".pdf"

    return ".bin"


def read_manifest(path: Path) -> list[Tournament]:
    tournaments: list[Tournament] = []

    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)

        if "DB-Key" not in (reader.fieldnames or []):
            raise ValueError("Manifest does not contain a DB-Key column.")

        seen: set[str] = set()

        for row in reader:
            db_key = clean_text(row.get("DB-Key"))

            if not db_key or not db_key.isdigit():
                continue

            if db_key in seen:
                continue

            seen.add(db_key)

            tournaments.append(
                Tournament(
                    db_key=db_key,
                    title=clean_text(row.get("Tournament")),
                    start_date=clean_text(row.get("from")),
                    end_date=clean_text(row.get("to")),
                    system=clean_text(row.get("System")),
                    source_file=clean_text(row.get("source_file")),
                )
            )

    return tournaments


def make_export_url(
    db_key: str,
    page_type: str,
    host: str,
    lan: str,
    excel_version: str,
) -> str:
    spec = EXPORT_TYPES[page_type]

    params: dict[str, str] = {
        "lan": lan,
        "zeilen": "0",
    }

    if spec["art"] is not None:
        params["art"] = str(spec["art"])

    if spec["turdet"]:
        params["turdet"] = "YES"

    params["prt"] = "4"
    params["excel"] = excel_version

    return f"https://{host}/tnr{db_key}.aspx?{urlencode(params)}"


def local_existing_file(tournament_dir: Path, db_key: str, page_type: str) -> Path | None:
    pattern = f"tnr{db_key}_{page_type}.*"
    matches = sorted(tournament_dir.glob(pattern))
    if matches:
        return matches[0]
    return None


def make_output_path(
    out_dir: Path,
    tournament: Tournament,
    page_type: str,
    extension: str,
) -> Path:
    folder_name = f"tnr{tournament.db_key}"
    tournament_dir = out_dir / "exports" / folder_name
    filename = f"tnr{tournament.db_key}_{page_type}{extension}"
    return tournament_dir / filename


def load_robots(host: str, user_agent: str, no_robots: bool):
    if no_robots:
        print("robots.txt check disabled by --no-robots")
        return None

    robots_url = f"https://{host}/robots.txt"
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)

    print(f"Reading robots.txt: {robots_url}")

    try:
        rp.read()
        return rp
    except Exception as e:
        print(f"Warning: could not read robots.txt: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def robot_allowed(rp, user_agent: str, url: str) -> bool:
    if rp is None:
        return True
    return bool(rp.can_fetch(user_agent, url))


def append_manifest_row(path: Path, row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "status",
        "db_key",
        "page_type",
        "title",
        "system",
        "from",
        "to",
        "source_file",
        "url",
        "final_url",
        "content_type",
        "local_path",
        "bytes",
        "sha256",
        "error",
    ]

    exists = path.exists()

    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not exists:
            writer.writeheader()

        writer.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser()

    ap.add_argument("--manifest", required=True, help="Path to chessresults_manifest.csv.")
    ap.add_argument("--out", required=True, help="Output folder.")
    ap.add_argument(
        "--types",
        default=",".join(DEFAULT_TYPES),
        help=(
            "Comma-separated export types. Known: "
            + ", ".join(EXPORT_TYPES.keys())
        ),
    )
    ap.add_argument("--only-db", default="", help="Download only one DB-Key.")
    ap.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max tournaments to process. Use 0 for no limit, but do that deliberately.",
    )
    ap.add_argument(
        "--start-after",
        default="",
        help="Skip DB-Keys until after this one. Useful for resuming by position.",
    )

    ap.add_argument("--host", default="s1.chess-results.com")
    ap.add_argument("--lan", default="1")
    ap.add_argument("--excel-version", default="2010")

    ap.add_argument("--delay", type=float, default=15.0)
    ap.add_argument("--jitter", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--retries", type=int, default=4)

    ap.add_argument("--refresh", action="store_true", help="Re-download files already present.")
    ap.add_argument("--plan-only", action="store_true", help="Only print/write the plan; do not download.")
    ap.add_argument("--no-robots", action="store_true", help="Do not check robots.txt.")
    ap.add_argument("--user-agent", default=USER_AGENT)

    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_manifest_path = out_dir / "download_manifest.csv"
    plan_path = out_dir / "planned_export_urls.txt"

    wanted_types = [x.strip() for x in args.types.split(",") if x.strip()]
    unknown = [x for x in wanted_types if x not in EXPORT_TYPES]
    if unknown:
        print(f"Unknown export type(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    tournaments = read_manifest(manifest_path)

    if args.only_db:
        tournaments = [t for t in tournaments if t.db_key == args.only_db.strip()]

    if args.start_after:
        found = False
        trimmed: list[Tournament] = []

        for t in tournaments:
            if found:
                trimmed.append(t)
            elif t.db_key == args.start_after.strip():
                found = True

        tournaments = trimmed

    if args.limit and args.limit > 0:
        tournaments = tournaments[: args.limit]

    if not tournaments:
        print("No tournaments selected.", file=sys.stderr)
        return 1

    planned: list[tuple[Tournament, str, str]] = []

    for tournament in tournaments:
        for page_type in wanted_types:
            url = make_export_url(
                db_key=tournament.db_key,
                page_type=page_type,
                host=args.host,
                lan=args.lan,
                excel_version=args.excel_version,
            )
            planned.append((tournament, page_type, url))

    with plan_path.open("w", encoding="utf-8") as f:
        for tournament, page_type, url in planned:
            f.write(f"{tournament.db_key}\t{page_type}\t{url}\n")

    print(f"Tournaments selected: {len(tournaments)}")
    print(f"Export types: {', '.join(wanted_types)}")
    print(f"Requests planned: {len(planned)}")
    print(f"Plan: {plan_path.resolve()}")

    if args.plan_only:
        print("Plan-only run complete. No files downloaded.")
        return 0

    rp = load_robots(args.host, args.user_agent, args.no_robots)

    fetcher = Fetcher(
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        user_agent=args.user_agent,
    )

    ok_count = 0
    skip_count = 0
    error_count = 0

    for i, (tournament, page_type, url) in enumerate(planned, start=1):
        tournament_dir = out_dir / "exports" / f"tnr{tournament.db_key}"

        existing = local_existing_file(tournament_dir, tournament.db_key, page_type)
        if existing and not args.refresh:
            print(f"[{i}/{len(planned)}] SKIP existing {existing.name}")
            skip_count += 1

            append_manifest_row(
                run_manifest_path,
                {
                    "status": "skipped_existing",
                    "db_key": tournament.db_key,
                    "page_type": page_type,
                    "title": tournament.title,
                    "system": tournament.system,
                    "from": tournament.start_date,
                    "to": tournament.end_date,
                    "source_file": tournament.source_file,
                    "url": url,
                    "final_url": "",
                    "content_type": "",
                    "local_path": str(existing),
                    "bytes": str(existing.stat().st_size),
                    "sha256": "",
                    "error": "",
                },
            )
            continue

        if not robot_allowed(rp, args.user_agent, url):
            print(f"[{i}/{len(planned)}] BLOCKED by robots.txt: {url}", file=sys.stderr)
            error_count += 1

            append_manifest_row(
                run_manifest_path,
                {
                    "status": "blocked_by_robots",
                    "db_key": tournament.db_key,
                    "page_type": page_type,
                    "title": tournament.title,
                    "system": tournament.system,
                    "from": tournament.start_date,
                    "to": tournament.end_date,
                    "source_file": tournament.source_file,
                    "url": url,
                    "final_url": "",
                    "content_type": "",
                    "local_path": "",
                    "bytes": "",
                    "sha256": "",
                    "error": "Blocked by robots.txt",
                },
            )
            continue

        print(
            f"[{i}/{len(planned)}] "
            f"tnr{tournament.db_key} {page_type}: {tournament.title[:70]}"
        )

        try:
            raw, final_url, content_type = fetcher.fetch(url)

            extension = detect_extension(raw, content_type)
            out_path = make_output_path(out_dir, tournament, page_type, extension)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)

            ok_count += 1

            append_manifest_row(
                run_manifest_path,
                {
                    "status": "ok",
                    "db_key": tournament.db_key,
                    "page_type": page_type,
                    "title": tournament.title,
                    "system": tournament.system,
                    "from": tournament.start_date,
                    "to": tournament.end_date,
                    "source_file": tournament.source_file,
                    "url": url,
                    "final_url": final_url,
                    "content_type": content_type,
                    "local_path": str(out_path),
                    "bytes": str(len(raw)),
                    "sha256": sha256_bytes(raw),
                    "error": "",
                },
            )

        except Exception as e:
            error_count += 1
            err = f"{type(e).__name__}: {e}"
            print(f"ERROR: {err}", file=sys.stderr)

            append_manifest_row(
                run_manifest_path,
                {
                    "status": "error",
                    "db_key": tournament.db_key,
                    "page_type": page_type,
                    "title": tournament.title,
                    "system": tournament.system,
                    "from": tournament.start_date,
                    "to": tournament.end_date,
                    "source_file": tournament.source_file,
                    "url": url,
                    "final_url": "",
                    "content_type": "",
                    "local_path": "",
                    "bytes": "",
                    "sha256": "",
                    "error": err,
                },
            )

    print()
    print("Done.")
    print(f"Downloaded: {ok_count}")
    print(f"Skipped:    {skip_count}")
    print(f"Errors:     {error_count}")
    print(f"HTTP fetches this run: {fetcher.fetch_count}")
    print(f"Output:     {out_dir.resolve()}")
    print(f"Log:        {run_manifest_path.resolve()}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())