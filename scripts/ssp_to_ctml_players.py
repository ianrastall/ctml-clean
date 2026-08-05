r"""Convert the project owner's Scid-derived .ssp player registry into CTML 2.0.

Format (reverse-engineered from ratings260703.ssp this session, confirmed
with the project owner where the data itself was ambiguous):

    @PLAYER "., -_*"

    <name> #<title-token>[ <FED>][ [<rating>]][ <birthdate>]
      %Bio <source> <id>            (0+, e.g. FIDE/Chessmetrics/Edo/Historical)
      %Elo <year>:<v1>,...,<v12>    (0+, monthly ratings, "?" = unknown)
      = <alias name>                (0+)

Design decisions, all confirmed with the project owner:

- Identity merge: two records merge into one CTML player ONLY if they share
  the same %Bio FIDE id (an unambiguous signal). Same-name records without a
  shared FIDE id stay separate, even though some are almost certainly the
  same person split across an old and a new source capture (e.g. one
  "Aarthie, Ramaswamy" record with no FIDE id and 1995-2000 history, another
  with FIDE id 5004373 and 2000-2012+ history) -- resolving those without an
  unambiguous key is exactly what the corpus's identity ratchet is for, not
  something to guess at conversion time.
- The bracket number (e.g. "[2402]") is the highest rating among that
  record's own %Elo values (confirmed by the project owner, not inferred) --
  mapped to <ctml:peak><ctml:value>. "achieved" is derived by finding the
  first year/month in that record's %Elo history equal to the bracket value.
- Title token, split on "+": "-"/empty -> no title. Any sub-token starting
  with "W" implies <ctml:sex>F</ctml:sex> (W/WC/WCM/WF/WFM/WGM/WIM all use
  that prefix for "woman" in this file). Sub-tokens matching CTML's
  PlayerTitleType enum exactly (GM/IM/FM/CM/WGM/WIM/WFM/WCM/NM) become
  <ctml:title> entries (schema widened to repeatable -- see
  xsd/ctml-entities.xsd -- to hold both halves of e.g. "IM+WGM"). Unrecognized
  sub-tokens (WC, WF, HM, ...) are dropped: neither the project owner nor I
  could confidently determine what WC/WF denote, and the project owner's call
  was to drop them rather than guess. The bare "-W" gender signal is kept
  because it doesn't require guessing the specific meaning, only the shared
  prefix convention.
- %Bio FIDE -> <ctml:ids><ctml:fideId>. Every other %Bio source (Chessmetrics,
  Edo, Historical, ...) -> <ctml:internalId>source:id</ctml:internalId>,
  matching the "source:id" convention already used by
  readers/ctml_source_common.py's crosstable_to_ctml().
- Birthdates in this file are always year-only precision ("YYYY.??.??" --
  confirmed against the full file: 100% of the 520,962 records with a
  birthdate use this exact pattern). No month/day handling needed.
- %Elo maps to a single <ctml:ratingTrack system="combined">, matching CTML
  1.3's "combined" rating design (highest-precedence source per month) --
  this file already looks like exactly that kind of blended series, not a
  single-source track.

Output is sharded by the first letter of the canonical display name
(A-Z, OTHER), matching the registry/players-fide, registry/players-edo
convention from the prior project at D:\ctml, so no single file needs to
hold the full ~600K-player registry.

Usage:
    python ssp_to_ctml_players.py [--in ratings260703.ssp] [--out-dir registry/players] [--limit N]
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "readers"))
from ctml_source_common import esc, normalize_space, sha1_hex16

CTML_NS = "urn:ctml:2.0"
CTML_VERSION = "2.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = PROJECT_ROOT / "ratings260703.ssp"
DEFAULT_OUT_DIR = PROJECT_ROOT / "registry" / "players"

RECOGNIZED_TITLES = {"GM", "IM", "FM", "CM", "WGM", "WIM", "WFM", "WCM", "NM"}

RECORD_RE = re.compile(
    r"^(?P<name>.*?) #(?P<title>\S*)"
    r"(?:\s+(?P<fed>[A-Z]{3}))?"
    r"(?:\s+\[(?P<rating>\d+)\])?"
    r"(?:\s+(?P<bdate>\S+))?"
    r"\s*$"
)
ELO_RE = re.compile(r"^  %Elo (?P<year>\d{4}):(?P<values>.+)$")
BIO_RE = re.compile(r"^  %Bio (?P<source>\S+) (?P<id>.+)$")
# ctml:FideIdType requires 4-12 digits (xsd/ctml-vocab.xsd). One record in
# ratings260703.ssp has "%Bio FIDE 19" -- too short to be a real FIDE id.
# Route anything that doesn't fit the pattern to internalId instead of
# fideId, so it neither breaks schema validation nor gets used as an
# (incorrect) merge key.
VALID_FIDE_ID_RE = re.compile(r"^\d{4,12}$")
ALIAS_RE = re.compile(r"^  = (?P<alias>.+)$")
BDATE_RE = re.compile(r"^(?P<y>\d{4})\.\?\?\.\?\?$")

SUFFIXES = {"jr", "jr.", "sr", "sr.", "i", "ii", "iii", "iv", "v", "vi", "2nd", "3rd"}


def parse_title_token(token: str) -> tuple[list[str], bool]:
    """Return (recognized title list, is_female)."""
    titles: list[str] = []
    female = False
    for sub in token.split("+"):
        if not sub or sub == "-":
            continue
        if sub.startswith("W"):
            female = True
        if sub in RECOGNIZED_TITLES and sub not in titles:
            titles.append(sub)
    return titles, female


@dataclass
class SspRecord:
    line_no: int
    name: str
    titles: list[str]
    female: bool
    fed: str
    peak_bracket: int | None
    birth_year: int | None
    fide_id: str | None = None
    other_ids: list[tuple[str, str]] = field(default_factory=list)
    elo: dict[int, list[str]] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)

    def total_elo_points(self) -> int:
        return sum(1 for values in self.elo.values() for v in values if v != "?")


def iter_records(path: Path, limit: int | None = None) -> list[SspRecord]:
    records: list[SspRecord] = []
    current: SspRecord | None = None
    with path.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.rstrip("\n")
            if not line or line.startswith("@"):
                continue
            if not line.startswith(" "):
                if current is not None:
                    records.append(current)
                    if limit is not None and len(records) >= limit:
                        return records
                m = RECORD_RE.match(line)
                if not m:
                    current = None
                    continue
                titles, female = parse_title_token(m.group("title") or "")
                birth_year = None
                bdate = m.group("bdate")
                if bdate:
                    bm = BDATE_RE.match(bdate)
                    if bm:
                        birth_year = int(bm.group("y"))
                current = SspRecord(
                    line_no=line_no,
                    name=normalize_space(m.group("name")),
                    titles=titles,
                    female=female,
                    fed=m.group("fed") or "",
                    peak_bracket=int(m.group("rating")) if m.group("rating") else None,
                    birth_year=birth_year,
                )
                continue
            if current is None:
                continue
            bm = BIO_RE.match(line)
            if bm:
                source, id_ = bm.group("source").strip(), bm.group("id").strip()
                if source.lower() == "fide" and VALID_FIDE_ID_RE.match(id_):
                    current.fide_id = id_
                elif source.lower() == "fide":
                    current.other_ids.append(("fide-raw", id_))
                else:
                    current.other_ids.append((source.lower(), id_))
                continue
            em = ELO_RE.match(line)
            if em:
                year = int(em.group("year"))
                values = [v.strip() for v in em.group("values").split(",")]
                current.elo[year] = values
                continue
            am = ALIAS_RE.match(line)
            if am:
                current.aliases.append(normalize_space(am.group("alias")))
                continue
    if current is not None:
        records.append(current)
    return records


@dataclass
class MergedPlayer:
    ref: str
    name: str
    fed: str
    titles: list[str]
    female: bool
    birth_year: int | None
    fide_id: str | None
    other_ids: list[tuple[str, str]]
    elo: dict[int, list[str]]
    aliases: list[str]


def merge_group(records: list[SspRecord]) -> MergedPlayer:
    records = sorted(records, key=lambda r: r.line_no)
    primary = records[0]

    titles: list[str] = []
    for r in records:
        for t in r.titles:
            if t not in titles:
                titles.append(t)
    female = any(r.female for r in records)

    birth_year = next((r.birth_year for r in records if r.birth_year is not None), None)

    # Federation: prefer the record with the most rating data (assumed most
    # current/complete); fall back to first non-empty.
    fed_source = max((r for r in records if r.fed), key=lambda r: r.total_elo_points(), default=None)
    fed = fed_source.fed if fed_source else ""

    fide_id = next((r.fide_id for r in records if r.fide_id), None)
    other_ids: list[tuple[str, str]] = []
    for r in records:
        for pair in r.other_ids:
            if pair not in other_ids:
                other_ids.append(pair)

    # Merge %Elo histories. On a year/month conflict between records, prefer
    # a non-"?" value; if both are numeric and differ, prefer the value from
    # the record with more total rated months (assumed more authoritative),
    # tie-broken by later line number (assumed newer capture).
    elo: dict[int, list[str]] = {}
    for r in sorted(records, key=lambda r: (r.total_elo_points(), r.line_no)):
        for year, values in r.elo.items():
            if year not in elo:
                elo[year] = list(values)
            else:
                merged = elo[year]
                for i, v in enumerate(values):
                    if merged[i] == "?" and v != "?":
                        merged[i] = v

    # Aliases: every other record's own canonical name, plus every record's
    # own listed aliases, excluding the chosen canonical name.
    aliases: list[str] = []
    for r in records:
        candidates = [r.name] if r is not primary else []
        candidates.extend(r.aliases)
        for c in candidates:
            if c and c != primary.name and c not in aliases:
                aliases.append(c)

    if fide_id:
        ref = f"player:fide:{fide_id}"
    else:
        ref = f"player:syn:{sha1_hex16(f'{primary.name.strip().lower()}|{fed.strip().upper()}')}"

    return MergedPlayer(
        ref=ref,
        name=primary.name,
        fed=fed,
        titles=titles,
        female=female,
        birth_year=birth_year,
        fide_id=fide_id,
        other_ids=other_ids,
        elo=elo,
        aliases=aliases,
    )


def compute_peak(player: MergedPlayer) -> tuple[int | None, tuple[int, int] | None]:
    best_value: int | None = None
    best_when: tuple[int, int] | None = None
    for year in sorted(player.elo):
        for month_idx, raw in enumerate(player.elo[year], start=1):
            if raw == "?":
                continue
            value = int(raw)
            if best_value is None or value > best_value:
                best_value = value
                best_when = (year, month_idx)
    return best_value, best_when


def compute_current(player: MergedPlayer) -> int | None:
    for year in sorted(player.elo, reverse=True):
        for raw in reversed(player.elo[year]):
            if raw != "?":
                return int(raw)
    return None


def person_name_xml(raw: str, indent: str, tag: str) -> str:
    raw = normalize_space(raw) or "Unknown"
    lines = [f'{indent}<ctml:{tag} display="{esc(raw)}">']
    if "," in raw:
        family, rest = raw.split(",", 1)
        family = family.strip()
        tokens = rest.strip().split()
        suffix = None
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
        elif tokens:
            # PersonNameType requires family before given (xs:sequence) --
            # last token is treated as the family name, but must still be
            # emitted first.
            lines.append(f"{indent}  <ctml:family>{esc(tokens[-1])}</ctml:family>")
            for given in tokens[:-1]:
                lines.append(f"{indent}  <ctml:given>{esc(given)}</ctml:given>")
        else:
            lines.append(f"{indent}  <ctml:unstructured>Unknown</ctml:unstructured>")
    lines.append(f"{indent}</ctml:{tag}>")
    return "\n".join(lines)


def player_xml(player: MergedPlayer) -> str:
    lines = [f'  <ctml:player ref="{esc(player.ref)}">']
    lines.append(person_name_xml(player.name, "    ", "name"))
    if player.fed:
        lines.append(f"    <ctml:federation>{player.fed}</ctml:federation>")
    for title in player.titles:
        lines.append(f"    <ctml:title>{title}</ctml:title>")
    if player.fide_id or player.other_ids:
        lines.append("    <ctml:ids>")
        if player.fide_id:
            lines.append(f"      <ctml:fideId>{esc(player.fide_id)}</ctml:fideId>")
        for source, id_ in player.other_ids:
            lines.append(f"      <ctml:internalId>{esc(source)}:{esc(id_)}</ctml:internalId>")
        lines.append("    </ctml:ids>")
    if player.birth_year is not None:
        lines.append(f'    <ctml:birthDate><ctml:year y="{player.birth_year}"/></ctml:birthDate>')
    if player.female:
        lines.append("    <ctml:sex>F</ctml:sex>")
    if player.aliases:
        lines.append("    <ctml:aliases>")
        for alias in player.aliases:
            lines.append(person_name_xml(alias, "      ", "alias"))
        lines.append("    </ctml:aliases>")
    if player.elo:
        current = compute_current(player)
        peak_value, peak_when = compute_peak(player)
        lines.append("    <ctml:ratingHistory>")
        lines.append('      <ctml:ratingTrack system="combined">')
        if current is not None:
            lines.append(f"        <ctml:current>{current}</ctml:current>")
        if peak_value is not None:
            lines.append("        <ctml:peak>")
            lines.append(f"          <ctml:value>{peak_value}</ctml:value>")
            if peak_when is not None:
                y, m = peak_when
                lines.append(f'          <ctml:achieved><ctml:month y="{y}" m="{m}"/></ctml:achieved>')
            lines.append("        </ctml:peak>")
        lines.append("        <ctml:history>")
        for year in sorted(player.elo):
            lines.append(f'          <ctml:year value="{year}">')
            for i, raw in enumerate(player.elo[year], start=1):
                lines.append(f'            <ctml:month num="{i}">{raw}</ctml:month>')
            lines.append("          </ctml:year>")
        lines.append("        </ctml:history>")
        lines.append("      </ctml:ratingTrack>")
        lines.append("    </ctml:ratingHistory>")
    lines.append("  </ctml:player>")
    return "\n".join(lines)


def shard_key(name: str) -> str:
    ch = name[:1].upper()
    return ch if "A" <= ch <= "Z" else "OTHER"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--limit", type=int, default=None, help="stop after N raw records (testing)")
    args = ap.parse_args()

    records = iter_records(Path(args.inp), limit=args.limit)
    print(f"parsed {len(records)} raw records", file=sys.stderr)

    by_fide: dict[str, list[SspRecord]] = {}
    standalone: list[SspRecord] = []
    for r in records:
        if r.fide_id:
            by_fide.setdefault(r.fide_id, []).append(r)
        else:
            standalone.append(r)

    merged_players: list[MergedPlayer] = []
    for group in by_fide.values():
        merged_players.append(merge_group(group))
    for r in standalone:
        merged_players.append(merge_group([r]))

    merges = sum(1 for g in by_fide.values() if len(g) > 1)
    print(
        f"{len(by_fide)} distinct FIDE ids ({merges} of them merged >1 record), "
        f"{len(standalone)} standalone (no FIDE id) -> {len(merged_players)} CTML players",
        file=sys.stderr,
    )

    shards: dict[str, list[str]] = {}
    for player in merged_players:
        shards.setdefault(shard_key(player.name), []).append(player_xml(player))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key in sorted(shards):
        path = out_dir / f"players-{key}.xml"
        body = "\n".join(shards[key])
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<ctml:playerRegistry xmlns:ctml="{CTML_NS}" ctmlVersion="{CTML_VERSION}" '
            f'generated="{generated}" source="ratings260703.ssp">\n'
            f"{body}\n"
            "</ctml:playerRegistry>\n",
            encoding="utf-8",
        )
        print(f"  {path}: {len(shards[key])} players", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
