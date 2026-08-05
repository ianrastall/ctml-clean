#!/usr/bin/env python3
r"""
Two-stage Chess-Results downloader with rating screening.

Stage 1:
    Download only the starting-rank Excel export for each tournament.

Stage 2:
    Parse the rating column and decide whether the tournament is worth keeping.

Stage 3:
    If accepted, download the rest of the useful exports.

No third-party packages required.

Example screen-only run:
    py .\screen-and-download-chess-results.py --manifest chessresults_manifest.csv --out chess-results-screened --limit 20 --gate top10_avg --threshold 2000 --screen-only --delay 15 --timeout 20

Strict all-players run:
    py .\screen-and-download-chess-results.py --manifest chessresults_manifest.csv --out chess-results-screened --limit 20 --gate strict_all_players --threshold 2000 --screen-only --delay 15 --timeout 20

Download accepted tournaments:
    py .\screen-and-download-chess-results.py --manifest chessresults_manifest.csv --out chess-results-screened --limit 20 --gate top10_avg --threshold 2000 --delay 15 --timeout 20
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import random
import re
import socket
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

USER_AGENT = (
    "ChessResultsRatingGateCollector/1.1 "
    "(single-user chess event archival research; slow single-threaded fetcher)"
)

KEEP_TYPES = [
    "final_ranking",
    "pairings_results",
    "final_crosstable",
    "starting_crosstable",
    "statistics",
    "playing_schedule",
]

EXPORT_TYPES = {
    "tournament_details": {
        "art": None,
        "turdet": True,
        "description": "Tournament details (HTML)",
        "is_html": True,
    },
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


@dataclass
class Tournament:
    db_key: str
    title: str
    start_date: str
    end_date: str
    system: str
    source_file: str


@dataclass
class RatingProfile:
    total_players: int
    rated_players: int
    unrated_players: int
    min_rating_all: int
    min_rating_rated: int
    max_rating: int
    avg_rating_all: float
    avg_rating_rated: float
    median_rating_rated: float
    top10_avg: float
    count_2000_plus: int
    count_below_threshold_all: int


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
        last_error: Exception | None = None

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
                    "Connection": "close",
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
                last_error = e
                retryable = e.code in {408, 429, 500, 502, 503, 504}

                if attempt <= self.retries and retryable:
                    wait = retry_wait(attempt, e.headers.get("Retry-After"))
                    print(f"HTTP {e.code}; retrying in {wait:.1f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue

                raise

            except (
                URLError,
                TimeoutError,
                socket.timeout,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                ConnectionResetError,
            ) as e:
                last_error = e

                if attempt <= self.retries:
                    wait = retry_wait(attempt, None)
                    print(
                        f"Network/read error; retrying in {wait:.1f}s: {url} "
                        f"({type(e).__name__}: {e})",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue

                raise

        raise RuntimeError(f"Fetch failed after retries: {url}; last error={last_error}")

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

    stripped = raw.lstrip()[:300].lower()

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
    }

    if not spec.get("is_html"):
        params["zeilen"] = "0"
        params["prt"] = "4"
        params["excel"] = excel_version
    else:
        params["zeilen"] = "99999"

    if spec["art"] is not None:
        params["art"] = str(spec["art"])

    if spec["turdet"]:
        params["turdet"] = "YES"

    return f"https://{host}/tnr{db_key}.aspx?{urlencode(params)}"


def local_path(
    out_dir: Path,
    db_key: str,
    page_type: str,
    extension: str = ".xlsx",
) -> Path:
    return out_dir / "exports" / f"tnr{db_key}" / f"tnr{db_key}_{page_type}{extension}"


def existing_export_path(out_dir: Path, db_key: str, page_type: str) -> Path | None:
    folder = out_dir / "exports" / f"tnr{db_key}"
    matches = sorted(folder.glob(f"tnr{db_key}_{page_type}.*"))
    return matches[0] if matches else None


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


def col_to_index(cell_ref: str) -> int:
    m = re.match(r"([A-Z]+)", cell_ref)
    if not m:
        return 0

    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)

    return n - 1


def read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []

    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    strings: list[str] = []

    for si in root.findall("a:si", NS):
        text = "".join(t.text or "" for t in si.findall(".//a:t", NS))
        strings.append(text)

    return strings


def parse_number_or_text(raw: str) -> object:
    raw = raw.strip()

    if raw == "":
        return ""

    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw

    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            return raw

    return raw


def read_cell_value(c: ET.Element, shared_strings: list[str]) -> object:
    cell_type = c.attrib.get("t")

    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in c.findall(".//a:t", NS)).strip()

    v = c.find("a:v", NS)

    if v is None or v.text is None:
        return ""

    raw = v.text

    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except Exception:
            return raw

    if cell_type == "str":
        return raw.strip()

    return parse_number_or_text(raw)


def read_xlsx_first_sheet(path: Path) -> list[list[object]]:
    with zipfile.ZipFile(path) as z:
        shared_strings = read_shared_strings(z)

        if "xl/worksheets/sheet1.xml" not in z.namelist():
            raise ValueError(f"No xl/worksheets/sheet1.xml in {path}")

        sheet_root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows: list[list[object]] = []

    for row in sheet_root.findall(".//a:sheetData/a:row", NS):
        cells: dict[int, object] = {}

        for c in row.findall("a:c", NS):
            ref = c.attrib.get("r", "A1")
            idx = col_to_index(ref)
            cells[idx] = read_cell_value(c, shared_strings)

        if cells:
            max_col = max(cells)
            rows.append([cells.get(i, "") for i in range(max_col + 1)])

    return rows


def normalize_header(value: object) -> str:
    s = str(value).strip().lower()
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def find_header_row(rows: list[list[object]]) -> tuple[int, list[str]]:
    """
    Find a plausible Chess-Results table header row.

    If the export has no rating column, this still returns a plausible table
    header where possible. The later parser then returns [] ratings, causing
    the tournament to be rejected rather than treated as a script error.
    """
    rating_headers = {
        "rtg",
        "rating",
        "elo",
        "fide rtg",
        "int. rating",
        "nat. rating",
        "rtgi",
        "rtgn",
        "rat",
        "elo fide",
    }

    name_headers = {
        "name",
        "player",
        "spieler",
        "nombre",
        "nome",
        "nom",
        "naam",
        "фамилия",
        "имя",
    }

    no_headers = {
        "no.",
        "no",
        "sno",
        "rank",
        "rk.",
        "start no",
        "startno",
    }

    for i, row in enumerate(rows):
        raw = [str(x).strip() for x in row]
        lowered = [normalize_header(x) for x in raw]

        has_rating = any(x in rating_headers for x in lowered)
        has_name = any(x in name_headers for x in lowered)
        has_number = any(x in no_headers for x in lowered)
        has_fed = any(x == "fed" for x in lowered)

        if has_rating:
            return i, raw

        if has_name and (has_number or has_fed):
            return i, raw

    return -1, []


def is_player_row(row: list[object], no_idx: int | None, name_idx: int | None) -> bool:
    if no_idx is not None and no_idx < len(row):
        no_value = str(row[no_idx]).strip()

        if re.fullmatch(r"\d+", no_value):
            return True

        if re.fullmatch(r"\d+\.", no_value):
            return True

    if name_idx is not None and name_idx < len(row):
        name_value = str(row[name_idx]).strip()

        if not name_value:
            return False

        lowered = name_value.lower()

        if "chess-results" in lowered:
            return False

        if "tournament" in lowered and "manager" in lowered:
            return False

        if "last update" in lowered:
            return False

        if len(name_value) >= 2:
            return True

    return False


def parse_rating_value(value: object) -> int:
    if value in ("", None):
        return 0

    if isinstance(value, (int, float)):
        rating = int(value)
        return rating if rating > 0 else 0

    s = str(value).strip()

    if not s:
        return 0

    # Some exports may contain e.g. "2025*" or "2025.0".
    m = re.search(r"\d{3,4}", s)
    if not m:
        return 0

    rating = int(m.group(0))

    # Avoid accidentally treating years, IDs, or small class numbers as ratings.
    if rating < 100:
        return 0

    return rating


def parse_ratings_from_starting_rank(path: Path) -> list[int]:
    """
    Return one rating value per player row.

    If a player row has an empty rating, it returns 0 for that player.
    If the table has no usable rating column at all, it returns [].
    """
    rows = read_xlsx_first_sheet(path)
    header_index, headers = find_header_row(rows)

    if header_index < 0 or not headers:
        return []

    normalized_headers = [normalize_header(h) for h in headers]

    rating_header_candidates = {
        "rtg",
        "rating",
        "elo",
        "fide rtg",
        "int. rating",
        "nat. rating",
        "rtgi",
        "rtgn",
        "rat",
        "elo fide",
    }

    name_header_candidates = {
        "name",
        "player",
        "spieler",
        "nombre",
        "nome",
        "nom",
        "naam",
        "фамилия",
        "имя",
    }

    number_header_candidates = {
        "no.",
        "no",
        "sno",
        "rank",
        "rk.",
        "start no",
        "startno",
    }

    rating_indexes = [
        i for i, h in enumerate(normalized_headers)
        if h in rating_header_candidates
    ]

    if "rtg" in normalized_headers:
        rating_indexes = [normalized_headers.index("rtg")]

    if not rating_indexes:
        return []

    name_idx = next(
        (i for i, h in enumerate(normalized_headers) if h in name_header_candidates),
        None,
    )

    no_idx = next(
        (i for i, h in enumerate(normalized_headers) if h in number_header_candidates),
        None,
    )

    ratings: list[int] = []

    for row in rows[header_index + 1:]:
        if not is_player_row(row, no_idx, name_idx):
            continue

        best_rating = 0

        for rtg_idx in rating_indexes:
            if rtg_idx >= len(row):
                continue

            rating = parse_rating_value(row[rtg_idx])

            if rating > best_rating:
                best_rating = rating

        ratings.append(best_rating)

    return ratings


def median(values: list[int]) -> float:
    if not values:
        return 0.0

    values = sorted(values)
    n = len(values)
    mid = n // 2

    if n % 2:
        return float(values[mid])

    return (values[mid - 1] + values[mid]) / 2.0


def build_rating_profile(ratings: list[int], threshold: int) -> RatingProfile:
    rated = [r for r in ratings if r > 0]
    top10 = sorted(rated, reverse=True)[:10]

    total = len(ratings)
    unrated = total - len(rated)

    return RatingProfile(
        total_players=total,
        rated_players=len(rated),
        unrated_players=unrated,
        min_rating_all=min(ratings) if ratings else 0,
        min_rating_rated=min(rated) if rated else 0,
        max_rating=max(ratings) if ratings else 0,
        avg_rating_all=(sum(ratings) / total) if total else 0.0,
        avg_rating_rated=(sum(rated) / len(rated)) if rated else 0.0,
        median_rating_rated=median(rated),
        top10_avg=(sum(top10) / len(top10)) if top10 else 0.0,
        count_2000_plus=sum(1 for r in ratings if r >= 2000),
        count_below_threshold_all=sum(1 for r in ratings if r < threshold),
    )


def parse_tour_avg_from_html(path: Path) -> int | None:
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
        table_match = re.search(r'<table class="CRs1".*?</table>', html, re.IGNORECASE | re.DOTALL)
        if not table_match:
            return None

        tr_tags = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(0), re.IGNORECASE | re.DOTALL)
        if not tr_tags:
            return None

        headers = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', tr_tags[0], re.IGNORECASE)
        rtg_idx = -1
        for i, h in enumerate(headers):
            if 'Rtg' in h:
                rtg_idx = i
                break

        if rtg_idx == -1:
            return None

        ratings = []
        for tr in tr_tags[1:]:
            cols = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.IGNORECASE)
            if len(cols) > rtg_idx:
                val = re.sub(r'<[^>]*>', '', cols[rtg_idx]).strip()
                if val.isdigit() and int(val) > 0:
                    ratings.append(int(val))

        if not ratings:
            return None

        return int(sum(ratings) / len(ratings))
    except Exception:
        pass
    return None


def decide_keep(profile: RatingProfile | None, gate: str, threshold: int, html_avg: int | None = None) -> tuple[bool, str]:
    if gate == "html_tour_avg":
        if html_avg is None:
            return False, "no average rating in HTML"
        if html_avg >= threshold:
            return True, f"html_avg {html_avg} >= {threshold}"
        return False, f"html_avg {html_avg} < {threshold}"

    if profile is None:
        return False, "no profile available"

    if profile.total_players == 0:
        return False, "no usable player rating column found"

    if gate == "strict_all_players":
        if profile.count_below_threshold_all > 0:
            return (
                False,
                f"{profile.count_below_threshold_all} players below {threshold}, including unrated/0",
            )
        return True, f"all players >= {threshold}"

    if gate == "avg_all":
        if profile.avg_rating_all >= threshold:
            return True, f"avg_all {profile.avg_rating_all:.1f} >= {threshold}"
        return False, f"avg_all {profile.avg_rating_all:.1f} < {threshold}"

    if gate == "avg_rated":
        if profile.rated_players == 0:
            return False, "no rated players"
        if profile.avg_rating_rated >= threshold:
            return True, f"avg_rated {profile.avg_rating_rated:.1f} >= {threshold}"
        return False, f"avg_rated {profile.avg_rating_rated:.1f} < {threshold}"

    if gate == "top10_avg":
        if profile.rated_players == 0:
            return False, "no rated players"
        if profile.top10_avg >= threshold:
            return True, f"top10_avg {profile.top10_avg:.1f} >= {threshold}"
        return False, f"top10_avg {profile.top10_avg:.1f} < {threshold}"

    if gate == "max_rating":
        if profile.max_rating >= threshold:
            return True, f"max_rating {profile.max_rating} >= {threshold}"
        return False, f"max_rating {profile.max_rating} < {threshold}"

    if gate == "count_2000_plus":
        if profile.count_2000_plus >= threshold:
            return True, f"count_2000_plus {profile.count_2000_plus} >= {threshold}"
        return False, f"count_2000_plus {profile.count_2000_plus} < {threshold}"

    raise ValueError(f"Unknown gate: {gate}")


def append_csv_row(path: Path, fieldnames: list[str], row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()

    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")

        if not exists:
            writer.writeheader()

        writer.writerow(row)


def write_screen_row(
    path: Path,
    tournament: Tournament,
    profile: RatingProfile | None,
    keep: bool,
    reason: str,
    starting_rank_path: str,
    error: str = "",
) -> None:
    fields = [
        "decision",
        "reason",
        "db_key",
        "title",
        "system",
        "from",
        "to",
        "total_players",
        "rated_players",
        "unrated_players",
        "min_rating_all",
        "min_rating_rated",
        "max_rating",
        "avg_rating_all",
        "avg_rating_rated",
        "median_rating_rated",
        "top10_avg",
        "count_2000_plus",
        "count_below_threshold_all",
        "starting_rank_path",
        "error",
    ]

    p = profile

    append_csv_row(
        path,
        fields,
        {
            "decision": "keep" if keep else "reject",
            "reason": reason,
            "db_key": tournament.db_key,
            "title": tournament.title,
            "system": tournament.system,
            "from": tournament.start_date,
            "to": tournament.end_date,
            "total_players": p.total_players if p else "",
            "rated_players": p.rated_players if p else "",
            "unrated_players": p.unrated_players if p else "",
            "min_rating_all": p.min_rating_all if p else "",
            "min_rating_rated": p.min_rating_rated if p else "",
            "max_rating": p.max_rating if p else "",
            "avg_rating_all": f"{p.avg_rating_all:.2f}" if p else "",
            "avg_rating_rated": f"{p.avg_rating_rated:.2f}" if p else "",
            "median_rating_rated": f"{p.median_rating_rated:.2f}" if p else "",
            "top10_avg": f"{p.top10_avg:.2f}" if p else "",
            "count_2000_plus": p.count_2000_plus if p else "",
            "count_below_threshold_all": p.count_below_threshold_all if p else "",
            "starting_rank_path": starting_rank_path,
            "error": error,
        },
    )


def write_download_row(
    path: Path,
    tournament: Tournament,
    page_type: str,
    status: str,
    url: str,
    final_url: str = "",
    content_type: str = "",
    local_file: str = "",
    raw: bytes | None = None,
    error: str = "",
) -> None:
    fields = [
        "status",
        "db_key",
        "page_type",
        "title",
        "system",
        "from",
        "to",
        "url",
        "final_url",
        "content_type",
        "local_path",
        "bytes",
        "sha256",
        "error",
    ]

    append_csv_row(
        path,
        fields,
        {
            "status": status,
            "db_key": tournament.db_key,
            "page_type": page_type,
            "title": tournament.title,
            "system": tournament.system,
            "from": tournament.start_date,
            "to": tournament.end_date,
            "url": url,
            "final_url": final_url,
            "content_type": content_type,
            "local_path": local_file,
            "bytes": len(raw) if raw is not None else "",
            "sha256": sha256_bytes(raw) if raw is not None else "",
            "error": error,
        },
    )


def download_export(
    tournament: Tournament,
    page_type: str,
    out_dir: Path,
    host: str,
    lan: str,
    excel_version: str,
    fetcher: Fetcher,
    rp,
    user_agent: str,
    refresh: bool,
    download_log_path: Path,
) -> Path:
    spec = EXPORT_TYPES[page_type]
    existing = existing_export_path(out_dir, tournament.db_key, page_type)

    url = make_export_url(
        db_key=tournament.db_key,
        page_type=page_type,
        host=host,
        lan=lan,
        excel_version=excel_version,
    )

    if existing and not refresh:
        if (spec.get("is_html") and existing.suffix.lower() == ".html") or \
           (not spec.get("is_html") and existing.suffix.lower() in [".xlsx", ".xls"]):
            write_download_row(
                path=download_log_path,
                tournament=tournament,
                page_type=page_type,
                status="skipped_existing",
                url=url,
                local_file=str(existing),
            )
            return existing

    if not robot_allowed(rp, user_agent, url):
        raise PermissionError(f"Blocked by robots.txt: {url}")

    raw, final_url, content_type = fetcher.fetch(url)
    ext = detect_extension(raw, content_type)

    out_path = local_path(out_dir, tournament.db_key, page_type, ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)

    write_download_row(
        path=download_log_path,
        tournament=tournament,
        page_type=page_type,
        status="ok",
        url=url,
        final_url=final_url,
        content_type=content_type,
        local_file=str(out_path),
        raw=raw,
    )

    if not spec.get("is_html") and ext not in [".xlsx", ".xls"]:
        raise ValueError(f"Expected Excel for {page_type}, got {ext}: {out_path}")
    if spec.get("is_html") and ext != ".html":
        raise ValueError(f"Expected HTML for {page_type}, got {ext}: {out_path}")

    return out_path


def select_tournaments(
    tournaments: list[Tournament],
    only_db: str,
    start_after: str,
    limit: int,
) -> list[Tournament]:
    if only_db:
        tournaments = [t for t in tournaments if t.db_key == only_db.strip()]

    if start_after:
        found = False
        trimmed: list[Tournament] = []

        for t in tournaments:
            if found:
                trimmed.append(t)
            elif t.db_key == start_after.strip():
                found = True

        tournaments = trimmed

    if limit and limit > 0:
        tournaments = tournaments[:limit]

    return tournaments


def write_key_lists(out_dir: Path, kept: list[str], rejected: list[str]) -> None:
    kept_path = out_dir / "kept_db_keys.txt"
    rejected_path = out_dir / "rejected_db_keys.txt"

    kept_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    rejected_path.write_text("\n".join(rejected) + ("\n" if rejected else ""), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()

    ap.add_argument("--manifest", default="chessresults_manifest.csv")
    ap.add_argument("--out", default="screened_tournaments")

    ap.add_argument(
        "--gate",
        choices=[
            "html_tour_avg",
            "strict_all_players",
            "avg_all",
            "avg_rated",
            "top10_avg",
            "max_rating",
            "count_2000_plus",
        ],
        default="html_tour_avg",
        help=(
            "Rating gate. For count_2000_plus, --threshold means required count, "
            "not rating value."
        ),
    )

    ap.add_argument("--threshold", type=int, default=2000)

    ap.add_argument("--only-db", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start-after", default="")

    ap.add_argument("--host", default="s1.chess-results.com")
    ap.add_argument("--lan", default="1")
    ap.add_argument("--excel-version", default="2010")

    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--jitter", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--retries", type=int, default=4)

    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--screen-only", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--no-robots", action="store_true")
    ap.add_argument("--user-agent", default=USER_AGENT)

    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    screening_log_path = out_dir / "screening_manifest.csv"
    download_log_path = out_dir / "download_manifest.csv"

    tournaments = read_manifest(manifest_path)
    tournaments = select_tournaments(
        tournaments=tournaments,
        only_db=args.only_db,
        start_after=args.start_after,
        limit=args.limit,
    )

    if not tournaments:
        print("No tournaments selected.", file=sys.stderr)
        return 1

    planned_screen_requests = len(tournaments)
    planned_keep_requests = len(tournaments) * len(KEEP_TYPES)

    print(f"Tournaments selected: {len(tournaments)}")
    print(f"Gate: {args.gate}")
    print(f"Threshold: {args.threshold}")
    print(f"Screen requests planned: {planned_screen_requests}")
    print(f"Maximum follow-up requests if every tournament passes: {planned_keep_requests}")

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

    kept: list[str] = []
    rejected: list[str] = []
    errors = 0

    for i, tournament in enumerate(tournaments, start=1):
        print(f"[{i}/{len(tournaments)}] Screening tnr{tournament.db_key}: {tournament.title[:100]}")

        try:
            if args.gate == "html_tour_avg":
                details_path = download_export(
                    tournament=tournament,
                    page_type="tournament_details",
                    out_dir=out_dir,
                    host=args.host,
                    lan=args.lan,
                    excel_version=args.excel_version,
                    fetcher=fetcher,
                    rp=rp,
                    user_agent=args.user_agent,
                    refresh=args.refresh,
                    download_log_path=download_log_path,
                )
                html_avg = parse_tour_avg_from_html(details_path)
                keep, reason = decide_keep(None, args.gate, args.threshold, html_avg=html_avg)
                profile = None
                starting_rank_path = ""
            else:
                sr_path = download_export(
                    tournament=tournament,
                    page_type="starting_rank",
                    out_dir=out_dir,
                    host=args.host,
                    lan=args.lan,
                    excel_version=args.excel_version,
                    fetcher=fetcher,
                    rp=rp,
                    user_agent=args.user_agent,
                    refresh=args.refresh,
                    download_log_path=download_log_path,
                )
                ratings = parse_ratings_from_starting_rank(sr_path)
                profile = build_rating_profile(ratings, args.threshold)
                keep, reason = decide_keep(profile, args.gate, args.threshold)
                starting_rank_path = str(sr_path)

            write_screen_row(
                path=screening_log_path,
                tournament=tournament,
                profile=profile,
                keep=keep,
                reason=reason,
                starting_rank_path=starting_rank_path,
            )

            if keep:
                kept.append(tournament.db_key)
                print(f"  KEEP: {reason}")

                if not args.screen_only:
                    extra_keep = KEEP_TYPES + (["starting_rank"] if args.gate == "html_tour_avg" else [])
                    for page_type in extra_keep:
                        print(f"    downloading {page_type}")

                        download_export(
                            tournament=tournament,
                            page_type=page_type,
                            out_dir=out_dir,
                            host=args.host,
                            lan=args.lan,
                            excel_version=args.excel_version,
                            fetcher=fetcher,
                            rp=rp,
                            user_agent=args.user_agent,
                            refresh=args.refresh,
                            download_log_path=download_log_path,
                        )

            else:
                rejected.append(tournament.db_key)
                print(f"  REJECT: {reason}")

        except KeyboardInterrupt:
            print()
            print("Interrupted by user. Writing key lists before exit...")
            write_key_lists(out_dir, kept, rejected)
            print(f"Kept so far:     {len(kept)}")
            print(f"Rejected so far: {len(rejected)}")
            print(f"HTTP fetches:    {fetcher.fetch_count}")
            return 130

        except Exception as e:
            errors += 1
            rejected.append(tournament.db_key)
            err = f"{type(e).__name__}: {e}"
            print(f"  ERROR: {err}", file=sys.stderr)

            write_screen_row(
                path=screening_log_path,
                tournament=tournament,
                profile=None,
                keep=False,
                reason="error during screening",
                starting_rank_path="",
                error=err,
            )

    write_key_lists(out_dir, kept, rejected)

    print()
    print("Done.")
    print(f"Kept:       {len(kept)}")
    print(f"Rejected:   {len(rejected)}")
    print(f"Errors:     {errors}")
    print(f"HTTP fetches this run: {fetcher.fetch_count}")
    print(f"Screen log: {screening_log_path.resolve()}")
    print(f"Download log: {download_log_path.resolve()}")
    print(f"Kept keys:  {(out_dir / 'kept_db_keys.txt').resolve()}")
    print(f"Rejected:   {(out_dir / 'rejected_db_keys.txt').resolve()}")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())