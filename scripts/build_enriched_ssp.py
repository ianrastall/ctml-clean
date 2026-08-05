#!/usr/bin/env python3
"""Build an enriched Scid spell-check file from the CTML registries.

The input SSP is treated as an opaque player-normalization artifact.  Its bytes
are copied unchanged, then generated SITE, EVENT, and ROUND sections are
appended.  CTML remains the identity authority: this builder emits an alias
only when its normalized spelling has exactly one target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEGACY_ROOT = Path(r"D:\ctml\sources")
GENERATED_BANNER = "# BEGIN CTML-GENERATED NON-PLAYER SECTIONS"
CONTROL_PREFIXES = ("@", "%", "=", ">")
SSP_EXCLUDE_TEXT = {
    "SITE": "., -_()",
    "EVENT": ",. -_",
    "ROUND": "",
}
SSP_EXCLUDES = {section: set(value) for section, value in SSP_EXCLUDE_TEXT.items()}


# These are the complete, context-free rules recoverable from the project
# notes.  The national-championship block contains an ellipsis in the recovered
# source, so that entire block is deliberately omitted rather than guessed.
EVENT_RULES: tuple[tuple[str, str, str], ...] = (
    ("Prefix", "01. ", "1. "),
    ("Prefix", "02. ", "2. "),
    ("Prefix", "03. ", "3. "),
    ("Prefix", "04. ", "4. "),
    ("Prefix", "05. ", "5. "),
    ("Prefix", "06. ", "6. "),
    ("Prefix", "07. ", "7. "),
    ("Prefix", "08. ", "8. "),
    ("Prefix", "09. ", "9. "),
    ("Prefix", "II ", "2. "),
    ("Prefix", "III ", "3. "),
    ("Prefix", "IV ", "4. "),
    ("Prefix", "V ", "5. "),
    ("Prefix", "VI ", "6. "),
    ("Prefix", "VII ", "7. "),
    ("Prefix", "VIII ", "8. "),
    ("Prefix", "IX ", "9. "),
    ("Infix", "1st ", "1. "),
    ("Infix", "2nd ", "2. "),
    ("Infix", "3rd ", "3. "),
    ("Infix", "4th ", "4. "),
    ("Infix", "5th ", "5. "),
    ("Infix", "6th ", "6. "),
    ("Infix", "7th ", "7. "),
    ("Infix", "8th ", "8. "),
    ("Infix", "9th ", "9. "),
    ("Infix", "0th ", "0. "),
    ("Infix", "11th ", "11. "),
    ("Infix", "12th ", "12. "),
    ("Infix", "13th ", "13. "),
    ("Suffix", " ch", " Ch"),
    ("Suffix", "-ch", " Ch"),
    ("Suffix", " ChT", " Team Ch"),
    ("Suffix", "-chT", " Team Ch"),
    ("Infix", "-ch ", " Ch "),
    ("Infix", "-chT ", " Team Ch "),
    ("Suffix", " Playoff", " playoff"),
    ("Suffix", " Play-Off", " playoff"),
    ("Suffix", " Play-off", " playoff"),
    ("Suffix", " play-off", " playoff"),
    ("Suffix", " plof", " playoff"),
    ("Suffix", " p/o", " playoff"),
    ("Suffix", " mem", " Memorial"),
    ("Suffix", " memorial", " Memorial"),
    ("Suffix", " Mem", " Memorial"),
    ("Infix", " mem ", " Memorial "),
    ("Suffix", " (team)", " team"),
    ("Suffix", " (Women)", " women"),
    ("Suffix", " (women)", " women"),
    ("Suffix", " Women", " women"),
    ("Suffix", " w", " women"),
    ("Suffix", " (w)", " women"),
    ("Suffix", " (Junior)", " junior"),
    ("Suffix", " (junior)", " junior"),
    ("Suffix", " Junior", " junior"),
    ("Suffix", " jr", " junior"),
    ("Suffix", " Jr", " junior"),
    ("Prefix", "corr ", "Corr "),
    ("Prefix", "cr ", "Corr "),
    ("Prefix", "Eu ", "Europe "),
    ("Prefix", "it ", "It "),
    ("Prefix", "m ", "Match "),
    ("Prefix", "match ", "Match "),
    ("Prefix", "open ", "Open "),
    ("Prefix", "Op ", "Open "),
    ("Suffix", " (open)", " Open"),
    ("Suffix", " open", " Open"),
    ("Suffix", " op", " Open"),
    ("Infix", " open ", " Open "),
    ("Infix", " op ", " Open "),
    ("Prefix", "wch", "WCh"),
    ("Prefix", "Zt ", "Zonal "),
    ("Prefix", "zt ", "Zonal "),
)


GENERIC_EVENT_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Corr", ("corr", "cr")),
    ("Interzonal", ("IZ", "izt", "IZT")),
    ("It", ("it",)),
    ("Match", ("match", "m")),
    ("Olympiad", ("ol",)),
    ("Olympiad (men)", ("ol (m)", "ol (men)", "Ol (men)")),
    ("Olympiad (women)", ("ol (w)", "ol (women)", "Ol (women)")),
    ("Open", ("open",)),
    ("Zonal", ("zonal", "Zone", "zone", "zt", "Zt", "ZT")),
    ("Candidates", ("Candidate",)),
    ("Tournament", ("tournament",)),
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def iter_child_text(element: ET.Element, names: set[str]) -> Iterator[str]:
    for child in element.iter():
        if local_name(child.tag) in names:
            value = (child.text or "").strip()
            if value:
                yield value


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def spelling_key(value: str, section: str) -> str:
    value = unicodedata.normalize("NFC", normalize_space(value)).casefold()
    excluded = SSP_EXCLUDES[section]
    return "".join(character for character in value if character not in excluded)


def safe_ssp_name(value: str) -> tuple[str | None, str | None]:
    value = normalize_space(value)
    if not value:
        return None, "empty"
    if "\ufffd" in value:
        return None, "contains_replacement_character"
    if "#" in value:
        return None, "contains_comment_marker"
    if value.startswith(CONTROL_PREFIXES):
        return None, "starts_with_control_marker"
    return value, None


@dataclass
class Diagnostics:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    examples: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def note(self, key: str, value: str | None = None) -> None:
        self.counts[key] += 1
        if value:
            self.examples[key].add(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "counts": dict(sorted(self.counts.items())),
            "examples": {
                key: sorted(values, key=lambda value: (value.casefold(), value))[:50]
                for key, values in sorted(self.examples.items())
            },
        }


@dataclass
class PlaceRecord:
    ref: str
    name: str
    country: str
    kind: str
    priority: int
    aliases: set[str] = field(default_factory=set)
    legacy_edo: bool = False


@dataclass
class EventRecord:
    name: str
    aliases: set[str]
    site_names: set[str]
    place_refs: set[str]
    place_names: set[str]
    priority: int


@dataclass
class Target:
    display: str
    priority: int
    spellings: set[str] = field(default_factory=set)


@dataclass
class Entry:
    canonical: str
    aliases: list[str]


def merge_place(existing: PlaceRecord | None, incoming: PlaceRecord) -> PlaceRecord:
    if existing is None:
        return incoming
    if (incoming.priority, incoming.name.casefold()) < (existing.priority, existing.name.casefold()):
        existing.name = incoming.name
        existing.priority = incoming.priority
    if not existing.country and incoming.country:
        existing.country = incoming.country
    if not existing.kind and incoming.kind:
        existing.kind = incoming.kind
    existing.aliases.update(incoming.aliases)
    existing.legacy_edo = existing.legacy_edo or incoming.legacy_edo
    return existing


def load_place_file(
    path: Path,
    priority: int,
    legacy_edo: bool,
    places: dict[str, PlaceRecord],
    diagnostics: Diagnostics,
) -> None:
    if not path.exists():
        diagnostics.note("missing_place_source", str(path))
        return
    loaded = 0
    for _event, element in ET.iterparse(path, events=("end",)):
        if local_name(element.tag) != "place":
            continue
        ref = element.get("ref", "").strip()
        name = child_text(element, "name")
        if not ref or not name:
            element.clear()
            continue
        aliases = set(iter_child_text(element, {"altName"}))
        record = PlaceRecord(
            ref=ref,
            name=name,
            country=child_text(element, "country").upper(),
            kind=element.get("kind", "").strip().lower(),
            priority=priority,
            aliases=aliases,
            legacy_edo=legacy_edo,
        )
        places[ref] = merge_place(places.get(ref), record)
        loaded += 1
        element.clear()
    diagnostics.counts["place_records_loaded"] += loaded


def load_places(
    active_place_files: Sequence[Path],
    edo_place_files: Sequence[Path],
    diagnostics: Diagnostics,
) -> dict[str, PlaceRecord]:
    places: dict[str, PlaceRecord] = {}
    for path in active_place_files:
        load_place_file(path, 0, False, places, diagnostics)
    for path in edo_place_files:
        load_place_file(path, 1, True, places, diagnostics)

    country_names: dict[str, set[str]] = defaultdict(set)
    for place in places.values():
        if place.kind == "country" and place.country:
            for name in {place.name, *place.aliases}:
                country_names[normalize_space(name).casefold()].add(place.country)

    for place in places.values():
        if place.country:
            continue
        for alias in place.aliases:
            if "," not in alias:
                continue
            suffix = normalize_space(alias.rsplit(",", 1)[1]).casefold()
            codes = country_names.get(suffix, set())
            if len(codes) == 1:
                place.country = next(iter(codes))
                diagnostics.note("place_country_inferred", f"{place.name} -> {place.country}")
                break
    return places


def load_event_file(path: Path, priority: int, diagnostics: Diagnostics) -> list[EventRecord]:
    if not path.exists():
        diagnostics.note("missing_event_source", str(path))
        return []
    tree = ET.parse(path)
    root = tree.getroot()
    records: list[EventRecord] = []
    series_names: dict[str, str] = {}

    for element in root.iter():
        if local_name(element.tag) != "eventSeries":
            continue
        ref = element.get("ref", "").strip()
        name = child_text(element, "name")
        if ref and name:
            series_names[ref] = name
            records.append(EventRecord(name, set(), set(), set(), set(), priority))

    for element in root.iter():
        if local_name(element.tag) != "eventOccurrence":
            continue
        name = child_text(element, "name")
        if not name:
            series_ref = child_text(element, "seriesRef")
            name = series_names.get(series_ref, "")
        aliases: set[str] = set()
        sites: set[str] = set()
        place_refs: set[str] = set()
        place_names: set[str] = set()
        for descendant in element.iter():
            tag = local_name(descendant.tag)
            if tag == "alias":
                alias = normalize_space(descendant.text or "")
                if alias:
                    aliases.add(alias)
                site = normalize_space(descendant.get("site", ""))
                if site:
                    sites.add(site)
            elif tag == "placeRef":
                ref = descendant.get("ref", "").strip()
                if ref:
                    place_refs.add(ref)
                nested_name = child_text(descendant, "name")
                if nested_name:
                    place_names.add(nested_name)
        if name:
            records.append(EventRecord(name, aliases, sites, place_refs, place_names, priority))

    diagnostics.counts["event_records_loaded"] += len(records)
    return records


def make_entries(
    canonical_and_aliases: Iterable[tuple[str, Iterable[str], int]],
    section: str,
    diagnostics: Diagnostics,
) -> list[Entry]:
    targets: dict[str, Target] = {}
    alias_claims: dict[str, set[str]] = defaultdict(set)
    alias_spellings: dict[tuple[str, str], set[str]] = defaultdict(set)

    for canonical_raw, aliases_raw, priority in canonical_and_aliases:
        canonical, reason = safe_ssp_name(canonical_raw)
        if canonical is None:
            diagnostics.note(f"{section.lower()}_unrepresentable_{reason}", canonical_raw)
            continue
        target_key = spelling_key(canonical, section)
        if not target_key:
            diagnostics.note(f"{section.lower()}_empty_normalized", canonical)
            continue
        target = targets.get(target_key)
        if target is None:
            target = Target(canonical, priority, {canonical})
            targets[target_key] = target
        else:
            target.spellings.add(canonical)
            if (priority, canonical.casefold(), canonical) < (
                target.priority,
                target.display.casefold(),
                target.display,
            ):
                target.display = canonical
                target.priority = priority

        for alias_raw in aliases_raw:
            alias, alias_reason = safe_ssp_name(alias_raw)
            if alias is None:
                diagnostics.note(f"{section.lower()}_unrepresentable_{alias_reason}", alias_raw)
                continue
            alias_key = spelling_key(alias, section)
            if not alias_key or alias_key == target_key:
                if alias != canonical:
                    target.spellings.add(alias)
                continue
            alias_claims[alias_key].add(target_key)
            alias_spellings[(alias_key, target_key)].add(alias)

    entries: list[Entry] = []
    for target_key, target in targets.items():
        aliases: set[str] = set(target.spellings)
        aliases.discard(target.display)
        for alias_key, claimed_targets in alias_claims.items():
            if target_key not in claimed_targets:
                continue
            if len(claimed_targets) != 1:
                diagnostics.note(
                    f"{section.lower()}_ambiguous_alias",
                    sorted(alias_spellings[(alias_key, target_key)], key=str.casefold)[0],
                )
                continue
            if alias_key in targets and alias_key != target_key:
                diagnostics.note(
                    f"{section.lower()}_alias_is_other_canonical",
                    sorted(alias_spellings[(alias_key, target_key)], key=str.casefold)[0],
                )
                continue
            aliases.update(alias_spellings[(alias_key, target_key)])
        entries.append(Entry(target.display, sorted(aliases, key=lambda value: (value.casefold(), value))))

    return sorted(entries, key=lambda entry: (entry.canonical.casefold(), entry.canonical))


def build_event_entries(records: Sequence[EventRecord], diagnostics: Diagnostics) -> list[Entry]:
    rows: list[tuple[str, Iterable[str], int]] = [
        (canonical, aliases, -1) for canonical, aliases in GENERIC_EVENT_NAMES
    ]
    rows.extend((record.name, record.aliases, record.priority) for record in records)
    return make_entries(
        rows,
        "EVENT",
        diagnostics,
    )


def place_groups(
    places: dict[str, PlaceRecord],
) -> tuple[
    dict[tuple[str, str], Target],
    dict[str, tuple[str, str]],
    dict[str, set[tuple[str, str]]],
]:
    countries_by_name: dict[str, set[str]] = defaultdict(set)
    for place in places.values():
        if place.country:
            countries_by_name[spelling_key(place.name, "SITE")].add(place.country)

    groups: dict[tuple[str, str], Target] = {}
    ref_to_key: dict[str, tuple[str, str]] = {}
    name_index: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for place in places.values():
        name_key = spelling_key(place.name, "SITE")
        country = place.country
        if not country and len(countries_by_name.get(name_key, set())) == 1:
            country = next(iter(countries_by_name[name_key]))
        key = (name_key, country)
        target = groups.get(key)
        if target is None:
            target = Target(place.name, place.priority, {place.name, *place.aliases})
            groups[key] = target
        else:
            target.spellings.update({place.name, *place.aliases})
            if (place.priority, place.name.casefold(), place.name) < (
                target.priority,
                target.display.casefold(),
                target.display,
            ):
                target.display = place.name
                target.priority = place.priority
        ref_to_key[place.ref] = key
        for spelling in {place.name, *place.aliases}:
            safe, _reason = safe_ssp_name(spelling)
            if safe:
                name_index[spelling_key(safe, "SITE")].add(key)
    return groups, ref_to_key, name_index


def build_site_entries(
    places: dict[str, PlaceRecord],
    events: Sequence[EventRecord],
    diagnostics: Diagnostics,
) -> list[Entry]:
    groups, ref_to_key, name_index = place_groups(places)
    used: set[tuple[str, str]] = set()
    extra_spellings: dict[tuple[str, str], set[str]] = defaultdict(set)

    for place in places.values():
        if place.legacy_edo and place.kind in {"city", "venue", ""}:
            key = ref_to_key.get(place.ref)
            if key:
                used.add(key)

    raw_sites: set[str] = set()
    for event in events:
        for ref in event.place_refs:
            key = ref_to_key.get(ref)
            if key:
                used.add(key)
            else:
                diagnostics.note("site_unresolved_place_ref", ref)
        raw_sites.update(event.place_names)
        raw_sites.update(event.site_names)

    for raw in sorted(raw_sites, key=lambda value: (value.casefold(), value)):
        safe, reason = safe_ssp_name(raw)
        if safe is None:
            diagnostics.note(f"site_unrepresentable_{reason}", raw)
            continue
        if safe.casefold() in {"?", "unknown", "n/a", "online"} or "://" in safe:
            diagnostics.note("site_nonphysical_or_unknown", safe)
            continue
        key_value = spelling_key(safe, "SITE")
        candidates = name_index.get(key_value, set())
        if len(candidates) == 1:
            key = next(iter(candidates))
            used.add(key)
            extra_spellings[key].add(safe)
        elif len(candidates) > 1:
            diagnostics.note("site_ambiguous_place_name", safe)
        else:
            key = (key_value, "")
            if key not in groups:
                groups[key] = Target(safe, 2, {safe})
            used.add(key)
            diagnostics.note("site_unresolved_created_standalone", safe)

    rows: list[tuple[str, set[str], int]] = []
    for key in used:
        target = groups[key]
        country = key[1]
        canonical = f"{target.display} {country}" if country else target.display
        aliases = set(target.spellings)
        aliases.update(extra_spellings.get(key, set()))
        if country:
            aliases.update(f"{alias} {country}" for alias in list(aliases))
        rows.append((canonical, aliases, target.priority))
    return make_entries(rows, "SITE", diagnostics)


def country_suffix_rules(places: dict[str, PlaceRecord], diagnostics: Diagnostics) -> list[tuple[str, str, str]]:
    claims: dict[str, set[str]] = defaultdict(set)
    display: dict[tuple[str, str], str] = {}
    for place in places.values():
        if place.kind != "country" or not place.country:
            continue
        for name in {place.name, *place.aliases}:
            safe, _reason = safe_ssp_name(name)
            if not safe or len(safe) < 3:
                continue
            key = normalize_space(safe).casefold()
            claims[key].add(place.country)
            display[(key, place.country)] = safe
    rules: list[tuple[str, str, str]] = []
    for key, codes in claims.items():
        if len(codes) != 1:
            diagnostics.note("site_ambiguous_country_suffix", key)
            continue
        code = next(iter(codes))
        rules.append(("Suffix", f"({display[(key, code)]})", code))
    return sorted(set(rules), key=lambda rule: (rule[1].casefold(), rule[1], rule[2]))


def escape_rule(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_section(
    section: str,
    entries: Sequence[Entry],
    rules: Sequence[tuple[str, str, str]] = (),
) -> list[str]:
    excluded = SSP_EXCLUDE_TEXT[section]
    lines = [f'@{section} "{excluded}"']
    for rule_type, source, replacement in rules:
        lines.append(f'%{rule_type} "{escape_rule(source)}" "{escape_rule(replacement)}"')
    for entry in entries:
        lines.append(entry.canonical)
        lines.extend(f"  = {alias}" for alias in entry.aliases)
    return lines


def build_round_entries(max_round: int) -> list[Entry]:
    entries: list[Entry] = []
    for number in range(1, max_round + 1):
        aliases = {f"({number})"}
        if number < 10:
            aliases.update({f"0{number}", f"(0{number})"})
        entries.append(Entry(str(number), sorted(aliases)))
    return entries


def render_generated_text(
    site_entries: Sequence[Entry],
    site_rules: Sequence[tuple[str, str, str]],
    event_entries: Sequence[Entry],
    round_entries: Sequence[Entry],
) -> str:
    lines = [
        GENERATED_BANNER,
        "# Generated by scripts/build_enriched_ssp.py; do not hand-edit.",
        "# SSP normalizes spellings. CTML remains the identity authority.",
        "",
    ]
    lines.extend(render_section("SITE", site_entries, site_rules))
    lines.append("")
    lines.extend(render_section("EVENT", event_entries, EVENT_RULES))
    lines.append("")
    lines.extend(render_section("ROUND", round_entries))
    lines.extend(["", "# END CTML-GENERATED NON-PLAYER SECTIONS", ""])
    return "\r\n".join(lines)


def inspect_base(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    markers: list[str] = []
    replacement_count = 0
    size = 0
    last_bytes = b""
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            size += len(line)
            replacement_count += line.count(b"\xef\xbf\xbd")
            last_bytes = line[-2:]
            stripped = line.lstrip()
            if stripped.startswith(b"@"):
                marker = stripped.split(None, 1)[0].decode("ascii", "replace")
                markers.append(marker)
    forbidden = [marker for marker in markers if marker.upper() in {"@SITE", "@EVENT", "@ROUND"}]
    if forbidden:
        raise ValueError(f"base SSP already contains generated section(s): {', '.join(forbidden)}")
    if "@PLAYER" not in {marker.upper() for marker in markers}:
        raise ValueError("base SSP has no @PLAYER section")
    return {
        "path": str(path.resolve()),
        "bytes": size,
        "sha256": digest.hexdigest(),
        "replacement_characters": replacement_count,
        "section_markers": markers,
        "ends_with_newline": last_bytes.endswith(b"\n"),
    }


def file_fingerprint(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(path.resolve()), "bytes": size, "sha256": digest.hexdigest()}


def validate_generated_text(text: str) -> dict[str, object]:
    markers: list[str] = []
    canonical_counts: dict[str, int] = defaultdict(int)
    alias_counts: dict[str, int] = defaultdict(int)
    rule_counts: dict[str, int] = defaultdict(int)
    current = ""
    has_canonical = False
    current_target = ""
    canonical_keys: dict[str, set[str]] = defaultdict(set)
    alias_targets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            current = line.split(None, 1)[0][1:]
            markers.append(current)
            has_canonical = False
            current_target = ""
        elif line.startswith("="):
            if not current or not has_canonical:
                raise ValueError(f"generated SSP alias without canonical name at line {number}")
            alias, reason = safe_ssp_name(line[1:].strip())
            if alias is None:
                raise ValueError(f"unrepresentable generated alias at line {number}: {reason}")
            alias_targets[current][spelling_key(alias, current)].add(current_target)
            alias_counts[current] += 1
        elif line.startswith("%"):
            if current not in {"SITE", "EVENT", "ROUND"}:
                raise ValueError(f"generated SSP rule outside a generated section at line {number}")
            if line.startswith(("%Bio", "%Elo")):
                raise ValueError(f"player metadata in {current} at line {number}")
            rule_counts[current] += 1
        else:
            if current not in {"SITE", "EVENT", "ROUND"}:
                raise ValueError(f"canonical name outside a generated section at line {number}")
            canonical, reason = safe_ssp_name(line)
            if canonical is None:
                raise ValueError(f"unrepresentable generated canonical at line {number}: {reason}")
            current_target = spelling_key(canonical, current)
            if current_target in canonical_keys[current]:
                raise ValueError(f"duplicate normalized {current} canonical at line {number}: {canonical}")
            canonical_keys[current].add(current_target)
            canonical_counts[current] += 1
            has_canonical = True
    if markers != ["SITE", "EVENT", "ROUND"]:
        raise ValueError(f"unexpected generated section order: {markers}")
    for section, aliases in alias_targets.items():
        for alias_key, targets in aliases.items():
            if len(targets) != 1:
                raise ValueError(f"generated {section} alias has multiple targets: {alias_key}")
            target = next(iter(targets))
            if alias_key in canonical_keys[section] and alias_key != target:
                raise ValueError(f"generated {section} alias shadows another canonical: {alias_key}")
    return {
        "section_markers": markers,
        "canonical_counts": dict(canonical_counts),
        "alias_counts": dict(alias_counts),
        "rule_counts": dict(rule_counts),
    }


def write_output(base: Path, output: Path, generated: bytes) -> tuple[str, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as destination, base.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if size and not chunk_ends_with_newline(base):
                destination.write(b"\r\n")
                digest.update(b"\r\n")
                size += 2
            destination.write(b"\r\n")
            destination.write(generated)
            digest.update(b"\r\n")
            digest.update(generated)
            size += 2 + len(generated)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temp_name, output)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return digest.hexdigest(), size


def chunk_ends_with_newline(path: Path) -> bool:
    if path.stat().st_size == 0:
        return False
    with path.open("rb") as stream:
        stream.seek(-1, os.SEEK_END)
        return stream.read(1) == b"\n"


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def existing_paths(paths: Iterable[Path]) -> list[Path]:
    return sorted({path.resolve() for path in paths if path.exists()}, key=lambda path: str(path).casefold())


def build(args: argparse.Namespace) -> dict[str, object]:
    base_path = args.base.resolve()
    output_path = args.output.resolve()
    report_path = args.report.resolve()
    if output_path == base_path:
        raise ValueError("output SSP must not overwrite the base SSP")
    if report_path in {base_path, output_path}:
        raise ValueError("report path must be distinct from both SSP paths")

    diagnostics = Diagnostics()
    base_info = inspect_base(args.base)

    active_place_files = existing_paths(args.active_places_dir.glob("*.xml"))
    edo_place_files = existing_paths([args.edo_places, args.edo_sites])
    places = load_places(active_place_files, edo_place_files, diagnostics)

    event_sources = [
        (args.active_events, 0),
        (args.edo_events, 1),
        (args.chessmetrics_events, 2),
    ]
    events: list[EventRecord] = []
    for path, priority in event_sources:
        events.extend(load_event_file(path, priority, diagnostics))

    site_entries = build_site_entries(places, events, diagnostics)
    event_entries = build_event_entries(events, diagnostics)
    round_entries = build_round_entries(args.max_round)
    site_rules = country_suffix_rules(places, diagnostics)
    generated_text = render_generated_text(site_entries, site_rules, event_entries, round_entries)
    validation = validate_generated_text(generated_text)
    generated = generated_text.encode("utf-8")
    generated_sha256 = hashlib.sha256(generated).hexdigest()

    output_sha256, output_bytes = write_output(args.base, args.output, generated)
    registry_paths = existing_paths(
        [*active_place_files, *edo_place_files, *(path for path, _priority in event_sources)]
    )
    report: dict[str, object] = {
        "format": "pgn-paladin-ssp-build-report-v1",
        "policy": {
            "ambiguous_aliases": "omitted",
            "identity_authority": "CTML",
            "player_section": "copied byte-for-byte",
            "replacement_characters_in_generated_sections": "omitted",
        },
        "base": base_info,
        "sources": {
            "files": [file_fingerprint(path) for path in registry_paths],
        },
        "generated": {
            "bytes": len(generated),
            "line_endings": "CRLF",
            "replacement_characters": generated_text.count("\ufffd"),
            "sha256": generated_sha256,
            "validation": validation,
        },
        "output": {
            "path": str(args.output.resolve()),
            "bytes": output_bytes,
            "sha256": output_sha256,
        },
        "diagnostics": diagnostics.as_dict(),
    }
    write_json_atomic(args.report, report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base", type=Path, default=PROJECT_ROOT / "ratings260703.ssp")
    result.add_argument("--active-events", type=Path, default=PROJECT_ROOT / "registry" / "events.xml")
    result.add_argument("--active-places-dir", type=Path, default=PROJECT_ROOT / "registry" / "places")
    result.add_argument(
        "--edo-events",
        type=Path,
        default=DEFAULT_LEGACY_ROOT / "edo" / "edo-events.xml",
    )
    result.add_argument(
        "--edo-places",
        type=Path,
        default=DEFAULT_LEGACY_ROOT / "edo" / "edo-places.xml",
    )
    result.add_argument(
        "--edo-sites",
        type=Path,
        default=DEFAULT_LEGACY_ROOT / "edo" / "edo-sites.xml",
    )
    result.add_argument(
        "--chessmetrics-events",
        type=Path,
        default=DEFAULT_LEGACY_ROOT / "chessmetrics" / "chessmetrics-events.xml",
    )
    result.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "build" / "ratings260703-enriched.ssp",
    )
    result.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "build" / "ratings260703-enriched.report.json",
    )
    result.add_argument("--max-round", type=int, default=200)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.max_round < 1:
        raise SystemExit("--max-round must be positive")
    report = build(args)
    print(json.dumps(report["generated"]["validation"], sort_keys=True))
    print(f"wrote {report['output']['path']}")
    print(f"wrote {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
