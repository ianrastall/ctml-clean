r"""Convert the dr5hn/countries-states-cities-database release assets into
CTML 2.0's place registry.

Source: reference/location-data/ -- the project owner dropped the .csv.gz
and .json.gz release assets from the latest release of
github.com/dr5hn/countries-states-cities-database there. Recognized by the
release-asset naming convention and the exact CSV/JSON schema.

Two source files are used (the rest -- postcodes, translations, geojson,
parquet, the mongodb dump -- are redundant with these for our purposes or
out of scope; see docs/HANDOFF.md for why each was skipped):

- json-countries+states+cities.json.gz: a clean nested country -> state
  hierarchy (250 countries, 5308 states). Used for the country and admin1
  registry entries. Country-level entries already carry a "translations"
  dict (language -> localized name) at no extra parsing cost, so country
  altNames are included; state/city altNames are not (that would need the
  separate, much larger translations.csv -- deferred, not essential for a
  first build).
- csv-cities.csv.gz: the flat city table (152,970 rows), joined back to the
  country/state hierarchy via its state_id/country_id foreign keys. Used
  instead of the nested JSON's "cities" arrays because it carries richer
  per-city data (wikiDataId, population, a "type" column) that the nested
  JSON's city objects don't include.

Mapping onto xsd/ctml-places.xsd:

- Country -> PlaceType, kind="country", ref="place:country:<iso3>",
  iso2/iso3/name/native latitude/longitude, altNames from the inline
  translations dict.
- State -> PlaceType, kind="admin1",
  ref="place:admin1:<iso3166_2>" (falls back to
  "place:admin1:<country iso3>-<state id>" for the 8 of 5308 states that
  have no iso3166_2 code), parentRef to its country.
- City row from csv-cities.csv.gz -> PlaceType. The "type" column is messy
  (35 distinct values: city/adm1/adm2/adm3/adm4/adm5/section/district/
  county/regency/prefecture/banner/town/village/... -- everything from
  real settlement-type labels to what look like data-entry artifacts).
  Simplified: type=="adm1" rows are skipped outright (redundant with the
  states already captured from the JSON); type=="adm2" -> kind="admin2"
  (17,801 rows, a real and common enough distinction to keep); everything
  else -> kind="city" (PlaceKindType has no slot finer than admin2, and for
  tournament-site matching purposes a district/county/settlement/etc. is
  functionally "a named place to match a Site string against", same as a
  city). ref="place:city:<country iso3>-<row id>" (or "place:admin2:..."
  for the admin2 case), parentRef to its state if the row's state_id
  resolves to a known state, else to its country directly.

Output is sharded to keep any single file manageable:
- registry/places/places-countries.xml (single file, 250 entries)
- registry/places/places-states.xml (single file, 5308 entries)
- registry/places/places-cities-<ISO3>.xml (one per country with cities)

Usage:
    python geodata_to_ctml_places.py [--data-dir reference/location-data] [--out-dir registry/places]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "readers"))
from ctml_source_common import esc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "reference" / "location-data"
DEFAULT_OUT_DIR = PROJECT_ROOT / "registry" / "places"

CTML_NS = "urn:ctml:2.0"
CTML_VERSION = "2.0"


def place_open(ref: str, kind: str) -> list[str]:
    return [f'  <ctml:place ref="{esc(ref)}" kind="{kind}">']


def alt_names_xml(translations: dict[str, str]) -> list[str]:
    if not translations:
        return []
    lines = ["    <ctml:altNames>"]
    for lang, text in sorted(translations.items()):
        lines.append(f'      <ctml:altName lang="{esc(lang)}">{esc(text)}</ctml:altName>')
    lines.append("    </ctml:altNames>")
    return lines


def country_xml(c: dict) -> tuple[str, str]:
    """Return (ref, xml)."""
    iso3 = c["iso3"]
    ref = f"place:country:{iso3}"
    lines = place_open(ref, "country")
    lines.append(f"    <ctml:name>{esc(c['name'])}</ctml:name>")
    # No <ctml:country> here: that field means "the country this place is
    # located in", which doesn't apply to a country-kind place itself.
    lines.append(f"    <ctml:iso2>{esc(c['iso2'])}</ctml:iso2>")
    lines.append(f"    <ctml:iso3>{esc(iso3)}</ctml:iso3>")
    if c.get("latitude") not in (None, ""):
        lines.append(f"    <ctml:latitude>{c['latitude']}</ctml:latitude>")
    if c.get("longitude") not in (None, ""):
        lines.append(f"    <ctml:longitude>{c['longitude']}</ctml:longitude>")
    lines.extend(alt_names_xml(c.get("translations") or {}))
    lines.append("  </ctml:place>")
    return ref, "\n".join(lines)


def state_ref(s: dict, country_iso3: str) -> str:
    code = s.get("iso3166_2")
    if code:
        return f"place:admin1:{code}"
    return f"place:admin1:{country_iso3}-{s['id']}"


def state_xml(s: dict, country_ref: str, country_iso3: str) -> tuple[str, str]:
    ref = state_ref(s, country_iso3)
    lines = place_open(ref, "admin1")
    lines.append(f"    <ctml:name>{esc(s['name'])}</ctml:name>")
    lines.append(f"    <ctml:country>{country_iso3}</ctml:country>")
    lines.append(f"    <ctml:admin1>{esc(s['name'])}</ctml:admin1>")
    lines.append(f"    <ctml:parentRef>{esc(country_ref)}</ctml:parentRef>")
    if s.get("latitude") not in (None, ""):
        lines.append(f"    <ctml:latitude>{s['latitude']}</ctml:latitude>")
    if s.get("longitude") not in (None, ""):
        lines.append(f"    <ctml:longitude>{s['longitude']}</ctml:longitude>")
    lines.append("  </ctml:place>")
    return ref, "\n".join(lines)


def city_xml(row: dict, country_iso3: str, state_name: str | None, parent_ref: str) -> tuple[str, str]:
    kind = "admin2" if row["type"] == "adm2" else "city"
    ref = f"place:{kind}:{country_iso3}-{row['id']}"
    lines = place_open(ref, kind)
    lines.append(f"    <ctml:name>{esc(row['name'])}</ctml:name>")
    lines.append(f"    <ctml:country>{country_iso3}</ctml:country>")
    if state_name:
        lines.append(f"    <ctml:admin1>{esc(state_name)}</ctml:admin1>")
    lines.append(f"    <ctml:parentRef>{esc(parent_ref)}</ctml:parentRef>")
    if row.get("wikiDataId"):
        lines.append(f"    <ctml:wikiDataId>{esc(row['wikiDataId'])}</ctml:wikiDataId>")
    if row.get("latitude"):
        lines.append(f"    <ctml:latitude>{row['latitude']}</ctml:latitude>")
    if row.get("longitude"):
        lines.append(f"    <ctml:longitude>{row['longitude']}</ctml:longitude>")
    lines.append("  </ctml:place>")
    return ref, "\n".join(lines)


def registry_wrap(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ctml:placeRegistry xmlns:ctml="{CTML_NS}" ctmlVersion="{CTML_VERSION}" '
        f'source="dr5hn/countries-states-cities-database">\n'
        f"{body}\n"
        "</ctml:placeRegistry>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with gzip.open(data_dir / "json-countries+states+cities.json.gz", "rt", encoding="utf-8") as f:
        countries = json.load(f)

    country_blocks: list[str] = []
    country_ref_by_id: dict[int, str] = {}
    country_iso3_by_id: dict[int, str] = {}
    state_ref_by_id: dict[int, str] = {}
    state_name_by_id: dict[int, str] = {}
    state_blocks: list[str] = []

    for c in countries:
        cref, cxml = country_xml(c)
        country_blocks.append(cxml)
        country_ref_by_id[c["id"]] = cref
        country_iso3_by_id[c["id"]] = c["iso3"]
        for s in c.get("states") or []:
            sref, sxml = state_xml(s, cref, c["iso3"])
            state_blocks.append(sxml)
            state_ref_by_id[s["id"]] = sref
            state_name_by_id[s["id"]] = s["name"]

    (out_dir / "places-countries.xml").write_text(registry_wrap("\n".join(country_blocks)), encoding="utf-8")
    (out_dir / "places-states.xml").write_text(registry_wrap("\n".join(state_blocks)), encoding="utf-8")
    print(f"countries: {len(country_blocks)}, states: {len(state_blocks)}", file=sys.stderr)

    city_shards: dict[str, list[str]] = {}
    skipped_adm1 = 0
    no_state = 0
    total = 0
    with gzip.open(data_dir / "csv-cities.csv.gz", "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["type"] == "adm1":
                skipped_adm1 += 1
                continue
            total += 1
            country_id = int(row["country_id"])
            country_iso3 = country_iso3_by_id.get(country_id)
            if not country_iso3:
                continue
            state_id = int(row["state_id"]) if row.get("state_id") else None
            parent_ref = state_ref_by_id.get(state_id) if state_id else None
            state_name = state_name_by_id.get(state_id) if state_id else None
            if parent_ref is None:
                parent_ref = country_ref_by_id[country_id]
                no_state += 1
            _, xml = city_xml(row, country_iso3, state_name, parent_ref)
            city_shards.setdefault(country_iso3, []).append(xml)

    for iso3 in sorted(city_shards):
        path = out_dir / f"places-cities-{iso3}.xml"
        path.write_text(registry_wrap("\n".join(city_shards[iso3])), encoding="utf-8")

    print(
        f"cities/admin2: {total} written across {len(city_shards)} country shards "
        f"({no_state} with no resolvable state -> parented directly to country), "
        f"{skipped_adm1} adm1-typed rows skipped (redundant with states.xml)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
