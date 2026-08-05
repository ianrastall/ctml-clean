r"""Convert a PGN database into CTML 2.0 tournament documents.

Parses games, groups them into tournaments, resolves participants/events/
places against the three registries built earlier this project (falling
back to synthetic refs and method="unresolved" wherever a resolution can't
be made confidently -- ambiguous or missing matches are meant to surface as
registry candidates for later curation, never guessed), applies the
corpus's rating-floor admission rule, and emits CTML tournament documents.

Tournament grouping: PGN databases (e.g. a Mega Database export) interleave
games from thousands of different events in no particular order. Games are
grouped by (normalized Event, normalized Site), then within each such group,
split into separate tournament occurrences wherever there's a date gap of
more than --max-gap-days (default 21) between consecutive games sorted by
date -- otherwise two different years of an annual "City Open" would merge
into one tournament spanning a year. 21 days is generous enough to keep a
single real event together (byes, split sessions) while safely separating
distinct editions of a series, which are almost always months apart.

Moves are stored as notation="uci": python-chess already walks the game
move-by-move to parse SAN into a Board (to validate the mainline and to
number plies), and UCI is what falls out of that walk directly -- no
separate hashing-time walk needed later (matches the phase-5 fingerprinting
design in docs/HANDOFF.md, which was written expecting exactly this).

Admission floor (docs/corpus-policy.md): a tournament is admitted only if
every participant has a known rating at or above --min-elo (default 2000,
matching the crosstables screening threshold). --min-elo 0 disables the
check entirely (useful for draft/inspection runs).

Registry resolution (player/event/place) is READ-ONLY in this script: it
matches against the existing registries but never writes new entries back
into them, even when nothing matches. Appending newly-discovered event
occurrences into the event registry is a separate, explicit, opt-in step
(--register-new-events, off by default) -- see docs/HANDOFF.md for why
this isn't automatic (TWIC's and Mega Database's naming conventions barely
overlap, so auto-registering every unresolved tournament would create
near-duplicate occurrences for events that already exist under a
different spelling).

Output is dedup-safe (readers/corpus_writer.py): --out-dir is a persistent
corpus directory, one file per event ref. Re-running against overlapping
or updated source data appends new games/participants into the existing
file for that event rather than creating a duplicate; a game already
present (same white/black/round slot) is matched via its trajectory
fingerprint, and a genuine divergence between sources (same slot,
different fingerprint) is logged, never silently overwritten -- existing
data always wins on conflict.

Usage:
    python pgn_to_ctml.py --input path\to\games.pgn --out-dir corpus\otb [--limit N] [--min-elo 2000]
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "readers"))
from ctml_source_common import PartialDate, esc, normalize_space, sha1_hex16, slug
from player_registry_index import PlayerIndex, build_index as build_player_index
from event_registry_index import EventIndex, build_index as build_event_index
from place_registry_index import PlaceIndex, build_index as build_place_index
from fingerprint import FingerprintAccumulator, fingerprints_xml
from corpus_writer import merge_tournament
from event_registry_writer import existing_refs, occurrence_xml, splice_registry

CTML_NS = "urn:ctml:2.0"
CTML_VERSION = "2.0"

GAME_RESULTS = {"1-0", "0-1", "1/2-1/2", "0-0", "*"}
TERMINATION_MAP = {
    "normal": "normal",
    "time forfeit": "timeout",
    "abandoned": "abandoned",
    "adjudication": "adjudication",
    "rules infraction": "forfeit",
    "unterminated": "unknown",
}
ECO_RE = re.compile(r"^[A-E][0-9]{2}$")


@dataclass
class ParsedGame:
    event: str
    site: str
    date: PartialDate | None
    round: str
    white: str
    black: str
    white_elo: str
    black_elo: str
    result: str
    eco: str
    termination: str
    uci_moves: list[str]
    trajectory_fp: str
    final_position_fp: str


def parse_pgn_date(raw: str) -> PartialDate | None:
    """PGN dates are "YYYY.MM.DD", but month and/or day are routinely "??"
    (unknown) -- an extremely common convention, not an edge case (roughly
    a quarter of games in mega-database-2025-filtered.pgn use it). Returns
    a PartialDate at whatever precision the source actually supports,
    rather than lying with a fabricated day. "????.??.??" (fully unknown)
    returns None."""
    m = re.fullmatch(r"(\d{4}|\?{4})\.(\d{2}|\?\?)\.(\d{2}|\?\?)", raw or "")
    if not m:
        return None
    y_s, mo_s, d_s = m.groups()
    if y_s == "????":
        return None
    y = int(y_s)
    if mo_s == "??":
        return PartialDate(y)
    mo = int(mo_s)
    if not 1 <= mo <= 12:
        return PartialDate(y)
    if d_s == "??":
        return PartialDate(y, mo)
    d = int(d_s)
    try:
        datetime.date(y, mo, d)
    except ValueError:
        return PartialDate(y, mo)
    return PartialDate(y, mo, d)


def approx_date(pd: PartialDate) -> datetime.date:
    """Fills unknown month/day with 1 for grouping/sorting/overlap
    comparisons only -- the true PartialDate (with its real precision) is
    what actually gets emitted into CTML output."""
    return datetime.date(max(1, pd.y), pd.m or 1, pd.d or 1)


def iter_games(path: Path, limit: int | None = None, skip: int = 0) -> Iterator[ParsedGame]:
    count = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        skipped = 0
        while skipped < skip:
            if chess.pgn.skip_game(f) is False:
                return
            skipped += 1
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                return
            h = game.headers
            board = game.board()
            fp_acc = FingerprintAccumulator(board)
            uci_moves: list[str] = []
            for move in game.mainline_moves():
                uci_moves.append(move.uci())
                board.push(move)
                fp_acc.update(board)
            trajectory_fp, final_position_fp = fp_acc.result()
            yield ParsedGame(
                event=normalize_space(h.get("Event", "")),
                site=normalize_space(h.get("Site", "")),
                date=parse_pgn_date(h.get("Date", "")),
                round=normalize_space(h.get("Round", "")) or "?",
                white=normalize_space(h.get("White", "")),
                black=normalize_space(h.get("Black", "")),
                white_elo=normalize_space(h.get("WhiteElo", "")),
                black_elo=normalize_space(h.get("BlackElo", "")),
                result=normalize_space(h.get("Result", "*")),
                eco=normalize_space(h.get("ECO", "")),
                termination=normalize_space(h.get("Termination", "")),
                uci_moves=uci_moves,
                trajectory_fp=trajectory_fp,
                final_position_fp=final_position_fp,
            )
            count += 1
            if limit is not None and count >= limit:
                return


@dataclass
class Tournament:
    event: str
    site: str
    start: PartialDate
    end: PartialDate
    dates_unknown: bool = False
    games: list[ParsedGame] = field(default_factory=list)


def group_tournaments(games: list[ParsedGame], max_gap_days: int) -> list[Tournament]:
    by_key: dict[tuple[str, str], list[ParsedGame]] = {}
    for g in games:
        key = (slug(g.event), slug(g.site))
        by_key.setdefault(key, []).append(g)

    tournaments: list[Tournament] = []
    for (_, _), group in by_key.items():
        dated = sorted((g for g in group if g.date), key=lambda g: approx_date(g.date))
        undated = [g for g in group if not g.date]

        runs: list[list[ParsedGame]] = []
        for g in dated:
            if runs and (approx_date(g.date) - approx_date(runs[-1][-1].date)).days <= max_gap_days:
                runs[-1].append(g)
            else:
                runs.append([g])
        if undated:
            # No date to anchor on: attach to the single existing run if
            # there is exactly one, otherwise keep as their own group
            # (can't safely decide which occurrence they belong to).
            if len(runs) == 1:
                runs[0].extend(undated)
            elif runs:
                runs.append(undated)
            else:
                runs = [undated]

        for run in runs:
            dated_in_run = [g for g in run if g.date]
            if dated_in_run:
                start = min((g.date for g in dated_in_run), key=approx_date)
                end = max((g.date for g in dated_in_run), key=approx_date)
                dates_unknown = False
            else:
                # No game in this run has a parseable date at all. Matches
                # the documented placeholder convention from the prior
                # project's pipeline (system-overview.md): 1970-01-01 with
                # a note, rather than silently dropping the games.
                start = end = PartialDate(1970, 1, 1)
                dates_unknown = True
            tournaments.append(
                Tournament(event=run[0].event, site=run[0].site, start=start, end=end, dates_unknown=dates_unknown, games=run)
            )
    return tournaments


def synth_player_ref(name: str) -> str:
    return f"player:syn:{sha1_hex16(name.strip().lower())}"


def person_name_xml(raw: str, indent: str, tag: str) -> str:
    raw = normalize_space(raw) or "Unknown"
    lines = [f'{indent}<ctml:{tag} display="{esc(raw)}">']
    if "," in raw:
        family, rest = raw.split(",", 1)
        lines.append(f"{indent}  <ctml:family>{esc(family.strip())}</ctml:family>")
        for given in rest.strip().split():
            lines.append(f"{indent}  <ctml:given>{esc(given)}</ctml:given>")
    else:
        tokens = raw.split()
        if len(tokens) <= 1:
            lines.append(f"{indent}  <ctml:family>{esc(raw)}</ctml:family>")
        else:
            lines.append(f"{indent}  <ctml:family>{esc(tokens[-1])}</ctml:family>")
            for given in tokens[:-1]:
                lines.append(f"{indent}  <ctml:given>{esc(given)}</ctml:given>")
    lines.append(f"{indent}</ctml:{tag}>")
    return "\n".join(lines)


def game_result_ok(result: str) -> str:
    return result if result in GAME_RESULTS else "*"


def tournament_xml(
    t: Tournament,
    tid: str,
    player_index: PlayerIndex | None,
    event_index: EventIndex | None,
    place_index: PlaceIndex | None,
    stats: dict,
    source_label: str,
) -> tuple[str, str, bool]:
    """Returns (xml_string, event_ref, event_was_resolved) -- the caller
    needs event_ref to decide which corpus file this tournament belongs
    in, and event_was_resolved to decide whether it's a candidate for
    --register-new-events."""
    event_ref, event_method = (None, "unresolved")
    if event_index is not None:
        event_ref, event_method = event_index.resolve(t.event, t.start, t.end)
    stats["events_total"] = stats.get("events_total", 0) + 1
    event_was_resolved = event_ref is not None
    if event_ref is None:
        event_ref = f"event:{t.start.compact()}-{t.end.compact()}-{slug(t.event)}"
    else:
        stats["events_resolved"] = stats.get("events_resolved", 0) + 1

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<ctml:tournament xmlns:ctml="{CTML_NS}" ctmlVersion="{CTML_VERSION}" id="{esc(tid)}">',
        "  <ctml:header>",
        f"    <ctml:name>{esc(t.event)}</ctml:name>",
        f'    <ctml:eventRef ref="{esc(event_ref)}"><ctml:name>{esc(t.event)}</ctml:name></ctml:eventRef>',
        "    <ctml:dates>",
        f"      {t.start.element('start')}",
        f"      {t.end.element('end')}",
        "    </ctml:dates>",
    ]
    if t.site:
        place_ref, place_method = (None, "unresolved")
        if place_index is not None:
            place_ref, place_method = place_index.resolve(t.site)
        stats["places_total"] = stats.get("places_total", 0) + 1
        if place_ref is None:
            place_ref = f"place:raw:{sha1_hex16(t.site.strip().lower())}"
        else:
            stats["places_resolved"] = stats.get("places_resolved", 0) + 1
        lines.append(f'    <ctml:placeRef ref="{esc(place_ref)}"><ctml:name>{esc(t.site)}</ctml:name></ctml:placeRef>')
    lines.append("  </ctml:header>")

    # Participants: one per distinct (name) appearing as White/Black in this group.
    participant_id: dict[str, str] = {}
    order: list[str] = []
    for g in t.games:
        for name in (g.white, g.black):
            if name and name not in participant_id:
                participant_id[name] = f"p{len(participant_id) + 1:04}"
                order.append(name)

    lines.append("  <ctml:participants>")
    for name in order:
        pid = participant_id[name]
        ref, method = (None, "unresolved")
        if player_index is not None:
            ref, method = player_index.resolve(name)
        stats["participants_total"] = stats.get("participants_total", 0) + 1
        if ref is None:
            ref = synth_player_ref(name)
        else:
            stats["participants_resolved"] = stats.get("participants_resolved", 0) + 1
        lines.append(f'    <ctml:participant id="{pid}">')
        lines.append(f'      <ctml:playerRef ref="{esc(ref)}">')
        lines.append(person_name_xml(name, "        ", "name"))
        lines.append(f'        <ctml:resolution method="{method}"/>')
        lines.append("      </ctml:playerRef>")
        lines.append("    </ctml:participant>")
    lines.append("  </ctml:participants>")

    lines.append("  <ctml:games>")
    for g in t.games:
        white_id = participant_id.get(g.white)
        black_id = participant_id.get(g.black)
        if not white_id or not black_id or white_id == black_id:
            continue  # unresolved/degenerate participant, skip rather than emit invalid data
        attrs = [f'round="{esc(g.round)}"', f'white="{white_id}"', f'black="{black_id}"', f'result="{esc(game_result_ok(g.result))}"']
        lines.append(f"    <ctml:game {' '.join(attrs)}>")
        if g.eco and ECO_RE.match(g.eco):
            lines.append(f"      <ctml:eco>{g.eco}</ctml:eco>")
        if g.uci_moves:
            lines.append('      <ctml:moves notation="uci">')
            for ply, mv in enumerate(g.uci_moves, start=1):
                lines.append(f'        <ctml:move ply="{ply}" value="{mv}"/>')
            lines.append("      </ctml:moves>")
        term = TERMINATION_MAP.get(g.termination.lower())
        if term:
            lines.append(f"      <ctml:termination>{term}</ctml:termination>")
        lines.append(fingerprints_xml(g.trajectory_fp, g.final_position_fp, indent="      "))
        lines.append(f'      <ctml:source kind="{esc(source_label)}"/>')
        lines.append("    </ctml:game>")
    lines.append("  </ctml:games>")
    if t.dates_unknown:
        lines.append("  <ctml:notes>Source PGN had no parseable date for any game in this group; 1970-01-01 is a placeholder, not a real date.</ctml:notes>")
    lines.append(f'  <ctml:source kind="{esc(source_label)}"/>')
    lines.append("</ctml:tournament>")
    lines.append("")
    return "\n".join(lines), event_ref, event_was_resolved


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAYERS_DIR = PROJECT_ROOT / "registry" / "players"
DEFAULT_EVENTS_PATH = PROJECT_ROOT / "registry" / "events.xml"
DEFAULT_PLACES_DIR = PROJECT_ROOT / "registry" / "places"


def admits(t: Tournament, min_elo: int) -> tuple[bool, str]:
    """Corpus admission rule (docs/corpus-policy.md): every participant must
    have a known rating at or above min_elo, or the whole tournament is
    excluded. min_elo <= 0 disables the check."""
    if min_elo <= 0:
        return True, ""
    lowest: dict[str, int | None] = {}
    for g in t.games:
        for name, elo in ((g.white, g.white_elo), (g.black, g.black_elo)):
            if not name:
                continue
            value = int(elo) if elo.isdigit() else None
            if name not in lowest:
                lowest[name] = value
            elif value is not None and (lowest[name] is None or value < lowest[name]):
                lowest[name] = value
    missing = [n for n, v in lowest.items() if v is None]
    if missing:
        return False, f"{len(missing)} of {len(lowest)} participants unrated"
    below = [n for n, v in lowest.items() if v < min_elo]
    if below:
        return False, f"{len(below)} of {len(lowest)} participants below {min_elo}"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--limit", type=int, default=None, help="stop after N games (testing)")
    ap.add_argument("--skip", type=int, default=0, help="skip the first N games (testing)")
    ap.add_argument("--max-gap-days", type=int, default=21)
    ap.add_argument("--min-elo", type=int, default=2000, help="corpus admission floor; 0 disables")
    ap.add_argument("--players-dir", default=str(DEFAULT_PLAYERS_DIR))
    ap.add_argument("--events-path", default=str(DEFAULT_EVENTS_PATH))
    ap.add_argument("--places-dir", default=str(DEFAULT_PLACES_DIR))
    ap.add_argument("--no-player-resolution", action="store_true")
    ap.add_argument("--no-event-resolution", action="store_true")
    ap.add_argument("--no-place-resolution", action="store_true")
    ap.add_argument("--source-label", default=None, help="kind= value for <ctml:source>; defaults to the input filename")
    ap.add_argument(
        "--register-new-events",
        action="store_true",
        help=(
            "Append tournaments whose event didn't match an existing registry occurrence as new "
            "eventOccurrence entries in registry/events.xml. OFF by default: TWIC's and Mega Database's "
            "naming conventions barely overlap (see docs/HANDOFF.md), so most 'unresolved' events here are "
            "not actually new -- they likely already exist in the registry under a different spelling. "
            "Auto-registering them would create near-duplicate occurrences. Only turn this on with that "
            "tradeoff in mind."
        ),
    )
    args = ap.parse_args()
    source_label = args.source_label or Path(args.input).name

    player_index = None
    if not args.no_player_resolution:
        print("building player registry index...", file=sys.stderr)
        player_index = build_player_index(Path(args.players_dir))
        print(f"  indexed {player_index.player_count} players", file=sys.stderr)

    event_index = None
    if not args.no_event_resolution:
        print("building event registry index...", file=sys.stderr)
        event_index = build_event_index(Path(args.events_path))
        print(f"  indexed {event_index.occurrence_count} occurrences", file=sys.stderr)

    place_index = None
    if not args.no_place_resolution:
        print("building place registry index...", file=sys.stderr)
        place_index = build_place_index(Path(args.places_dir))
        print(f"  indexed {place_index.place_count} places", file=sys.stderr)

    games = list(iter_games(Path(args.input), limit=args.limit, skip=args.skip))
    print(f"parsed {len(games)} games", file=sys.stderr)

    tournaments = group_tournaments(games, args.max_gap_days)
    print(f"grouped into {len(tournaments)} tournaments", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats: dict = {}
    admitted = 0
    rejected = 0
    created = 0
    merged = 0
    divergent_tournaments = 0
    unresolved_for_registration: list[Tournament] = []
    for t in tournaments:
        ok, reason = admits(t, args.min_elo)
        if not ok:
            rejected += 1
            continue
        admitted += 1
        tid = f"t_{slug(t.event)}_{t.start.compact()}"
        xml_str, event_ref, event_was_resolved = tournament_xml(
            t, tid, player_index, event_index, place_index, stats, source_label
        )
        if args.register_new_events and not event_was_resolved:
            unresolved_for_registration.append(t)

        def log(msg: str) -> None:
            nonlocal divergent_tournaments
            divergent_tournaments += 1
            print(msg, file=sys.stderr)

        status = merge_tournament(out_dir, xml_str, event_ref, log, max_gap_days=args.max_gap_days)
        if status == "created":
            created += 1
        else:
            merged += 1
            print(f"{event_ref}: {status}", file=sys.stderr)

    print(
        f"admitted {admitted} of {len(tournaments)} tournaments (floor {args.min_elo}): "
        f"{created} new files, {merged} merged into existing files, wrote to {out_dir}",
        file=sys.stderr,
    )

    if args.register_new_events and unresolved_for_registration:
        events_path = Path(args.events_path)
        existing_series, existing_occurrences = existing_refs(events_path.read_text(encoding="utf-8"))
        by_ref: dict[str, Tournament] = {}
        for t in unresolved_for_registration:
            ref = f"event:{t.start.compact()}-{t.end.compact()}-{slug(t.event)}"
            by_ref.setdefault(ref, t)  # first-seen wins if this run has near-duplicates
        new_series: dict[str, str] = {}
        new_occurrence_blocks: list[str] = []
        for ref, t in sorted(by_ref.items()):
            if ref in existing_occurrences:
                continue
            series_ref = f"series:{slug(t.event)}"
            if series_ref not in existing_series and series_ref not in new_series:
                new_series[series_ref] = t.event
            aliases = [(t.event, source_label, t.site)]
            new_occurrence_blocks.append(
                occurrence_xml(ref, series_ref, t.event, aliases, "", t.start, t.end, [f"{source_label}"])
            )
        splice_registry(events_path, new_series, new_occurrence_blocks)
        print(
            f"registered {len(new_occurrence_blocks)} new event occurrences, {len(new_series)} new series "
            f"into {events_path} (from {len(unresolved_for_registration)} unresolved tournaments this run)",
            file=sys.stderr,
        )
    if stats.get("participants_total"):
        print(
            f"player resolution: {stats.get('participants_resolved', 0)} / {stats['participants_total']} "
            f"participant slots resolved to a registry ref",
            file=sys.stderr,
        )
    if stats.get("events_total"):
        print(
            f"event resolution: {stats.get('events_resolved', 0)} / {stats['events_total']} "
            f"tournaments matched an existing registry occurrence",
            file=sys.stderr,
        )
    if stats.get("places_total"):
        print(
            f"place resolution: {stats.get('places_resolved', 0)} / {stats['places_total']} "
            f"sites matched an existing registry place",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
