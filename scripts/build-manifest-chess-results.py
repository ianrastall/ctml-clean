#!/usr/bin/env python3
"""
Build a Chess-Results tournament manifest from TournamentSearch.xlsx exports.

No pandas. No openpyxl. Uses only the Python standard library.

Input:
    A folder containing one or more Chess-Results TournamentSearch .xlsx files.

Output:
    chessresults_manifest.csv
    chessresults_seed_urls.txt

The seed URLs include the major public page types:
    main
    final_ranking        art=1
    pairings_results     art=2
    final_crosstable     art=4
    starting_crosstable  art=5
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

PAGE_TYPES = [
    ("main", ""),
    ("final_ranking", "art=1"),
    ("pairings_results", "art=2"),
    ("final_crosstable", "art=4"),
    ("starting_crosstable", "art=5"),
]


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


def read_sheet_rows(xlsx_path: Path) -> list[list[object]]:
    with zipfile.ZipFile(xlsx_path) as z:
        shared_strings = read_shared_strings(z)
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows_by_number: dict[int, dict[int, object]] = {}

    for row in root.findall(".//a:sheetData/a:row", NS):
        row_number = int(row.attrib["r"])
        cells: dict[int, object] = {}

        for c in row.findall("a:c", NS):
            ref = c.attrib.get("r", "A1")
            idx = col_to_index(ref)
            cell_type = c.attrib.get("t")
            v = c.find("a:v", NS)

            value: object = None
            if v is not None and v.text is not None:
                raw = v.text

                if cell_type == "s":
                    value = shared_strings[int(raw)]
                else:
                    try:
                        value = int(raw) if re.fullmatch(r"-?\d+", raw) else float(raw)
                    except ValueError:
                        value = raw

            cells[idx] = value

        rows_by_number[row_number] = cells

    if not rows_by_number:
        return []

    max_col = max(
        (idx for cells in rows_by_number.values() for idx in cells.keys()),
        default=0,
    )

    rows: list[list[object]] = []
    for row_number in sorted(rows_by_number):
        cells = rows_by_number[row_number]
        rows.append([cells.get(i) for i in range(max_col + 1)])

    return rows


def find_header_row(rows: list[list[object]]) -> tuple[int, list[str]]:
    for i, row in enumerate(rows):
        normalized = [str(x).strip() if x is not None else "" for x in row]
        if "DB-Key" in normalized and "Tournament" in normalized:
            return i, normalized

    raise ValueError("Could not find header row containing Tournament and DB-Key")


def clean_value(value: object) -> str:
    if value is None:
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def make_url(db_key: str, query: str) -> str:
    base = f"https://chess-results.com/tnr{db_key}.aspx?lan=1"
    if query:
        return f"{base}&{query}"
    return base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "input_folder",
        help="Folder containing TournamentSearch .xlsx files.",
    )
    ap.add_argument(
        "--out",
        default="chessresults-manifest",
        help="Output folder.",
    )
    args = ap.parse_args()

    input_folder = Path(args.input_folder)
    out_folder = Path(args.out)
    out_folder.mkdir(parents=True, exist_ok=True)

    xlsx_files = sorted(input_folder.glob("*.xlsx"))
    if not xlsx_files:
        print(f"No .xlsx files found in {input_folder}", file=sys.stderr)
        return 1

    manifest_path = out_folder / "chessresults_manifest.csv"
    urls_path = out_folder / "chessresults_seed_urls.txt"

    records_by_db_key: dict[str, dict[str, str]] = {}

    for xlsx_path in xlsx_files:
        print(f"Reading {xlsx_path}")

        rows = read_sheet_rows(xlsx_path)
        header_index, headers = find_header_row(rows)

        try:
            db_idx = headers.index("DB-Key")
        except ValueError:
            print(f"Skipping {xlsx_path}: no DB-Key column", file=sys.stderr)
            continue

        for row in rows[header_index + 1 :]:
            padded = row + [None] * max(0, len(headers) - len(row))
            values = {
                headers[i]: clean_value(padded[i])
                for i in range(len(headers))
                if headers[i]
            }

            db_key = values.get("DB-Key", "").strip()
            if not db_key or not db_key.isdigit():
                continue

            values["source_file"] = xlsx_path.name
            values["main_url"] = make_url(db_key, "")

            for page_name, query in PAGE_TYPES:
                values[f"url_{page_name}"] = make_url(db_key, query)

            # Deduplicate across the 13 files.
            # Keep the first version, but this can be changed later if you prefer
            # latest Last update wins.
            records_by_db_key.setdefault(db_key, values)

    records = list(records_by_db_key.values())

    if not records:
        print("No tournament records found.", file=sys.stderr)
        return 1

    core_fields = [
        "source_file",
        "Tournament",
        "from",
        "to",
        "EventID",
        "Organizer(s)",
        "Tournament director",
        "Chief Arbiter",
        "Deputy Chief Arbiter",
        "Arbiter",
        "Location",
        "Time control",
        "FED",
        "State",
        "Last update ",
        "teams",
        "n",
        "Rd",
        "Rd-Akt",
        "DB-Key",
        "System",
    ]

    url_fields = [f"url_{page_name}" for page_name, _ in PAGE_TYPES]
    fieldnames = core_fields + url_fields

    with manifest_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    with urls_path.open("w", encoding="utf-8") as f:
        for record in records:
            db_key = record["DB-Key"]
            for page_name, query in PAGE_TYPES:
                f.write(f"{make_url(db_key, query)}\n")

    print()
    print("Done.")
    print(f"Input files:   {len(xlsx_files)}")
    print(f"Tournaments:   {len(records)}")
    print(f"Manifest:      {manifest_path.resolve()}")
    print(f"Seed URLs:     {urls_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())