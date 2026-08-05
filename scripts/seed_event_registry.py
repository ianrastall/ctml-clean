r"""Seed the event registry from screened (admitted) crosstables.

Ported from D:\ctml\scripts\seed_event_registry.py to target CTML 2.0. No
splicing-logic changes were needed: this script never depended on
PartialDateType's internal shape directly, it only calls
PartialDate.element('start'|'end') from ctml_source_common, which already
emits the new CTML 2.0 nested year/month/day-choice shape (see
readers/ctml_source_common.py and xsd/ctml-dates.xsd). Default paths point
at this project's own build/ and registry/ instead of D:\ctml's.

Each admitted crosstable becomes an eventOccurrence with the stable ref
`event:<startYYYYMMDD>-<endYYYYMMDD>-<slug>`, a seriesRef on the slugged
event name (one draft series per distinct name -- curation merges series
later), and an alias per contributing source carrying the observed spelling
and raw place text. Records from different sources that resolve to the same
occurrence ref merge into one entry -- that is the point of the registry.

New entries are spliced into the existing registry file (via
readers/event_registry_writer.py, shared with scripts/pgn_to_ctml.py's
--register-new-events path so both sources use one splice mechanism),
preserving whatever is already there; occurrence refs already present are
left untouched.

Usage:
    python seed_event_registry.py [--in build\crosstables-strict2000.json] [--registry registry\events.xml]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "readers"))
from ctml_source_common import PartialDate, map_event_format, normalize_space, parse_iso_date, slug
from event_registry_writer import existing_refs, occurrence_xml, splice_registry


def sanitize_date(date: PartialDate) -> PartialDate:
    """Degrade precision rather than emit impossible dates (TWIC prints
    things like '33rd March'; the schema rightly rejects them)."""
    if date.m is not None and not 1 <= date.m <= 12:
        return PartialDate(date.y)
    if date.d is not None and not date.is_calendar_day():
        return PartialDate(date.y, date.m)
    return date

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = PROJECT_ROOT / "build" / "crosstables-strict2000.json"
DEFAULT_REGISTRY = PROJECT_ROOT / "registry" / "events.xml"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args()

    with Path(args.inp).open(encoding="utf-8") as handle:
        tables = json.load(handle)

    registry_path = Path(args.registry)
    existing_series, existing_occurrences = existing_refs(registry_path.read_text(encoding="utf-8"))

    # Group crosstables by occurrence identity.
    occurrences: dict[str, dict] = {}
    skipped_unnamed = 0
    skipped_undated = 0
    for table in tables:
        event = normalize_space(table.get("event", ""))
        event_slug = slug(event)
        if not event or event == "?" or event_slug == "unknown":
            skipped_unnamed += 1
            continue
        start = parse_iso_date(table.get("start") or table.get("end"))
        end = parse_iso_date(table.get("end") or table.get("start"))
        if start is None or end is None:
            skipped_undated += 1
            continue
        start, end = sanitize_date(start), sanitize_date(end)
        if start.is_calendar_day() and end.is_calendar_day() and (start.y, start.m, start.d) > (end.y, end.m, end.d):
            start, end = end, start
        ref = f"event:{start.compact()}-{end.compact()}-{event_slug}"
        entry = occurrences.setdefault(
            ref,
            {
                "series_ref": f"series:{event_slug}",
                "name": event,
                "aliases": [],
                "event_type": map_event_format(table.get("format", "")),
                "start": start,
                "end": end,
                "notes": [],
            },
        )
        source = table.get("source", "?")
        site = normalize_space(table.get("place", ""))
        alias = (event, source, site)
        if alias not in entry["aliases"]:
            entry["aliases"].append(alias)
        provenance = f"{source} {table.get('ref', '')}".strip()
        if provenance not in entry["notes"]:
            entry["notes"].append(provenance)

    new_series: dict[str, str] = {}
    new_occurrence_blocks: list[str] = []
    for ref, entry in sorted(occurrences.items()):
        if ref in existing_occurrences:
            continue
        series_ref = entry["series_ref"]
        if series_ref not in existing_series and series_ref not in new_series:
            new_series[series_ref] = entry["name"]
        event_type = entry["event_type"] if entry["event_type"] != "unknown" else ""
        new_occurrence_blocks.append(
            occurrence_xml(
                ref, series_ref, entry["name"], entry["aliases"],
                event_type, entry["start"], entry["end"], entry["notes"],
            )
        )

    splice_registry(registry_path, new_series, new_occurrence_blocks)

    print(f"input: {len(tables)} admitted crosstables -> {len(occurrences)} distinct occurrences", file=sys.stderr)
    print(f"added {len(new_occurrence_blocks)} occurrences, {len(new_series)} series to {registry_path}", file=sys.stderr)
    print(f"skipped: {skipped_unnamed} unnamed, {skipped_undated} undated", file=sys.stderr)
    merged = sum(1 for e in occurrences.values() if len(e["notes"]) > 1)
    print(f"cross-source merges: {merged} occurrences fed by more than one record", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
