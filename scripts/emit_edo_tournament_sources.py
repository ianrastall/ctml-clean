#!/usr/bin/env python3
"""Emit Edo tournament result rows as CTML tournament source drafts.

This is intentionally a source-draft emitter, not a corpus-policy gate. It
preserves Edo tournament rosters and scores whether or not every participant is
2000+, so later curation can accept or reject whole tournaments with evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape


NS = {"ctml": "urn:ctml:1.0"}
SOURCE_URL = "https://www.edochess.ca"
SUFFIXES = {"jr", "jr.", "sr", "sr.", "i", "ii", "iii", "iv", "v", "vi", "2nd", "3rd"}


@dataclass(frozen=True)
class EdoDate:
    y: int
    m: int | None = None
    d: int | None = None

    @property
    def precision(self) -> str:
        if self.m is not None and self.d is not None:
            return "day"
        if self.m is not None:
            return "month"
        return "year"

    def compact(self) -> str:
        return f"{self.y:04}{self.m or 0:02}{self.d or 0:02}"

    def is_calendar_day(self) -> bool:
        if self.m is None or self.d is None:
            return False
        leap = self.y % 4 == 0 and (self.y % 100 != 0 or self.y % 400 == 0)
        month_days = {
            1: 31,
            2: 29 if leap else 28,
            3: 31,
            4: 30,
            5: 31,
            6: 30,
            7: 31,
            8: 31,
            9: 30,
            10: 31,
            11: 30,
            12: 31,
        }
        return 1 <= self.d <= month_days.get(self.m, 0)

    def element(self, tag: str) -> str:
        attrs = [f'y="{self.y}"']
        if self.m is not None:
            attrs.append(f'm="{self.m}"')
        if self.d is not None:
            attrs.append(f'd="{self.d}"')
        attrs.append(f'precision="{self.precision}"')
        if self.is_calendar_day():
            attrs.append(f'iso="{self.y:04}-{self.m:02}-{self.d:02}"')
        return f"<ctml:{tag} {' '.join(attrs)}/>"


def month_number(name: str) -> int | None:
    key = name.rstrip(".").lower()[:3]
    return {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }.get(key)


def parse_edo_date(raw: str | None) -> EdoDate | None:
    if not raw:
        return None
    parts = raw.strip().split()
    if len(parts) == 3:
        try:
            d = int(parts[0])
            m = month_number(parts[1])
            y = int(parts[2])
        except ValueError:
            return None
        if m and 1 <= d <= 31:
            return EdoDate(y, m, d)
    if len(parts) == 2:
        m = month_number(parts[0])
        try:
            y = int(parts[1])
        except ValueError:
            return None
        if m:
            return EdoDate(y, m, None)
    if len(parts) == 1:
        try:
            y = int(parts[0])
        except ValueError:
            return None
        if 1000 <= y <= 2100:
            return EdoDate(y)
    return None


def esc(value: object | None) -> str:
    return xml_escape("" if value is None else str(value), {'"': "&quot;"})


def slug(value: str) -> str:
    out: list[str] = []
    pending_dash = False
    for ch in value.strip().lower():
        if ch.isalnum():
            if pending_dash and out:
                out.append("-")
            pending_dash = False
            out.append(ch)
        elif ch.isspace() or ch in "_-":
            pending_dash = True
    return "".join(out) or "unknown"


def sha1_hex16(data: str) -> str:
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def place_city_ref(city: str, country: str) -> str:
    return f"place:city:{sha1_hex16(f'{city.strip().lower()}|{country.strip().lower()}')}"


def clean_geo(value: str) -> str:
    value = value.strip()
    if "(" in value:
        value = value.split("(", 1)[0].strip()
    return value.rstrip(")").strip()


def parse_location(name: str, heading: str | None) -> tuple[str, str | None, str | None]:
    city = (name or "").strip()
    tail = (heading or "").strip()
    if tail.startswith("Events taking place in"):
        tail = tail[len("Events taking place in") :].strip()
    segments = [clean_geo(s) for s in tail.split(",")]
    segments = [s for s in segments if s]
    if not city and segments:
        city = segments[0]
    if not segments:
        return city, None, None
    rest = segments[1:] if segments and segments[0] == city else segments
    country = rest[-1] if rest else segments[-1]
    region = ", ".join(rest[:-1]) if len(rest) > 1 else None
    return city, region, country


def person_name_xml(raw: str, indent: str) -> str:
    raw = raw.strip() or "Unknown"
    lines = [f'{indent}<ctml:name display="{esc(raw)}">']
    if "," in raw:
        family, rest = raw.split(",", 1)
        family = family.strip()
        rest = rest.strip()
        suffix = None
        tokens = rest.split()
        if tokens and tokens[-1].lower() in SUFFIXES:
            suffix = tokens.pop()
        lines.append(f"{indent}  <ctml:family>{esc(family)}</ctml:family>")
        for given in tokens:
            lines.append(f"{indent}  <ctml:given>{esc(given)}</ctml:given>")
        if suffix:
            lines.append(f"{indent}  <ctml:suffix>{esc(suffix)}</ctml:suffix>")
    else:
        tokens = raw.split()
        if len(tokens) == 1:
            lines.append(f"{indent}  <ctml:family>{esc(tokens[0])}</ctml:family>")
        else:
            for given in tokens[:-1]:
                lines.append(f"{indent}  <ctml:given>{esc(given)}</ctml:given>")
            lines.append(f"{indent}  <ctml:family>{esc(tokens[-1])}</ctml:family>")
    lines.append(f"{indent}</ctml:name>")
    return "\n".join(lines)


def load_event_refs(events_xml: Path) -> dict[str, str]:
    tree = ET.parse(events_xml)
    out: dict[str, str] = {}
    for occ in tree.findall(".//ctml:eventOccurrence", NS):
        ref = occ.attrib.get("ref")
        notes = occ.findtext("ctml:notes", default="", namespaces=NS)
        match = re.fullmatch(r"Edo tournament (t\d+)", notes.strip())
        if ref and match:
            out[match.group(1)] = ref
    return out


def rows_by_tournament(con: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    sql = """
        SELECT *
        FROM tournament_result_rows
        WHERE row_kind = 'score'
        ORDER BY tournament_id, row_index
    """
    for row in con.execute(sql):
        grouped.setdefault(row["tournament_id"], []).append(row)
    return grouped


def load_locations(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["location_id"]: row
        for row in con.execute("SELECT * FROM locations ORDER BY location_id")
    }


def participant_note(row: sqlite3.Row) -> str | None:
    parts = []
    for label, key in (("games", "games"), ("dev", "dev"), ("class", "class")):
        value = row[key]
        if value is not None:
            parts.append(f"{label}={value}")
    if row["note"]:
        parts.append(str(row["note"]))
    return "; ".join(parts) if parts else None


def render_tournament(
    t: sqlite3.Row,
    result_rows: list[sqlite3.Row],
    locations: dict[str, sqlite3.Row],
    event_refs: dict[str, str],
) -> str | None:
    start = parse_edo_date(t["start_date_raw"])
    if not start:
        return None
    end = parse_edo_date(t["end_date_raw"]) or start
    name = t["page_title"] or t["name"] or t["tournament_id"]
    tid = f"t_edo_{t['tournament_id']}"
    event_ref = event_refs.get(t["tournament_id"])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ctml:tournament xmlns:ctml="urn:ctml:1.0" ctmlVersion="1.3" id="{esc(tid)}">',
        "  <ctml:header>",
        f"    <ctml:name>{esc(name)}</ctml:name>",
    ]
    if event_ref:
        lines.extend(
            [
                f'    <ctml:eventRef ref="{esc(event_ref)}">',
                f"      <ctml:name>{esc(name)}</ctml:name>",
                "    </ctml:eventRef>",
            ]
        )
    lines.extend(["    <ctml:dates>", f"      {start.element('start')}", f"      {end.element('end')}", "    </ctml:dates>"])

    loc = locations.get(t["place_id"] or "")
    if loc:
        city, _region, country = parse_location(loc["name"], loc["heading"])
        if city:
            pref = place_city_ref(city, country or "")
            lines.append(f'    <ctml:placeRef ref="{esc(pref)}" kind="city">')
            lines.append(f"      <ctml:name>{esc(city)}</ctml:name>")
            lines.append("    </ctml:placeRef>")
    lines.append("  </ctml:header>")

    lines.append("  <ctml:participants>")
    for idx, row in enumerate(result_rows, start=1):
        pid = f"p{idx:04}"
        player_id = row["player_id"] or ""
        ref = f"player:edo:{player_id[1:]}" if re.fullmatch(r"p\d+", player_id) else ""
        synthetic = False
        if not ref:
            ref = f"player:syn:{sha1_hex16((row['player_name'] or '').strip().lower() + '|')}"
            synthetic = True
        lines.append(f'    <ctml:participant id="{pid}">')
        lines.append(f'      <ctml:playerRef ref="{esc(ref)}" source="{SOURCE_URL}">')
        lines.append(person_name_xml(row["player_name"] or "Unknown", "        "))
        if player_id:
            lines.extend(
                [
                    "        <ctml:ids>",
                    f"          <ctml:internalId>edo:{esc(player_id[1:] if player_id.startswith('p') else player_id)}</ctml:internalId>",
                    "        </ctml:ids>",
                ]
            )
        if synthetic:
            lines.append('        <ctml:resolution method="unresolved" resolver="emit_edo_tournament_sources/0.1"/>')
        lines.append("      </ctml:playerRef>")
        if row["edo"] is not None:
            lines.extend(
                [
                    '      <ctml:ratingSnapshot system="edo" scope="standard">',
                    f"        <ctml:value>{int(row['edo'])}</ctml:value>",
                    f"        {start.element('asOf')}",
                    "      </ctml:ratingSnapshot>",
                ]
            )
        if row["score"] is not None:
            lines.append(f"      <ctml:score>{row['score']:g}</ctml:score>")
        note = participant_note(row)
        if note:
            lines.append(f"      <ctml:notes>{esc(note)}</ctml:notes>")
        lines.append("    </ctml:participant>")
    lines.append("  </ctml:participants>")

    notes = []
    if t["name"] and t["name"] != t["page_title"]:
        notes.append(f"Edo event name: {t['name']}")
    if t["notes"]:
        notes.append(str(t["notes"]))
    if notes:
        lines.append(f"  <ctml:notes>{esc(' '.join(notes))}</ctml:notes>")
    lines.extend(
        [
            '  <ctml:source kind="edo">',
            f"    <ctml:uri>{esc(t['source_url'])}</ctml:uri>",
            f"    <ctml:note>tournament_id={esc(t['tournament_id'])}; source_path={esc(t['source_path'])}</ctml:note>",
            "  </ctml:source>",
            "</ctml:tournament>",
            "",
        ]
    )
    return "\n".join(lines)


def clear_xml_files(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.xml"):
        path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"D:\edo\edo_registry.sqlite")
    ap.add_argument("--events", default=r"D:\ctml\sources\edo\edo-events.xml")
    ap.add_argument("--out", default=r"D:\ctml\build\drafts\edo")
    args = ap.parse_args()

    db_path = Path(args.db)
    events_path = Path(args.events)
    out_dir = Path(args.out)

    event_refs = load_event_refs(events_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    locations = load_locations(con)
    grouped = rows_by_tournament(con)

    clear_xml_files(out_dir)
    written = 0
    skipped_undated = 0
    skipped_empty = 0

    for t in con.execute("SELECT * FROM tournaments ORDER BY tournament_id"):
        rows = grouped.get(t["tournament_id"], [])
        if not rows:
            skipped_empty += 1
            continue
        xml = render_tournament(t, rows, locations, event_refs)
        if xml is None:
            skipped_undated += 1
            continue
        filename = f"{t['tournament_id']}-{slug(t['page_title'] or t['name'] or t['tournament_id'])}.xml"
        (out_dir / filename).write_text(xml, encoding="utf-8")
        written += 1

    con.close()
    print(f"wrote {written} Edo tournament CTML source drafts to {out_dir}")
    print(f"skipped {skipped_undated} undated tournaments and {skipped_empty} empty tournaments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
