from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLD = 2000


@dataclass
class SourceSummary:
    source: str
    input: str
    output: str
    total_events: int = 0
    kept_events: int = 0
    rejected_events: int = 0
    total_rows: int = 0
    kept_rows: int = 0
    rejected_rows: int = 0
    missing_or_unrated_rows: int = 0
    below_threshold_rows: int = 0
    notes: str = ""


def parse_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rate_values(values: list[int | None], threshold: int) -> tuple[bool, str, int, int, int | None, int | None]:
    missing = sum(1 for value in values if value is None)
    below = sum(1 for value in values if value is not None and value < threshold)
    numeric = [value for value in values if value is not None]
    min_rating = min(numeric) if numeric else None
    max_rating = max(numeric) if numeric else None
    if not values:
        return False, "no player rows", missing, below, min_rating, max_rating
    if missing:
        return False, f"{missing} missing/unrated player ratings", missing, below, min_rating, max_rating
    if below:
        return False, f"{below} player ratings below {threshold}", missing, below, min_rating, max_rating
    return True, f"all {len(values)} player ratings >= {threshold}", missing, below, min_rating, max_rating


def cull_twic_json(path: Path, out_dir: Path, threshold: int) -> SourceSummary | None:
    if not path.exists():
        return None

    ensure_dir(out_dir)
    out_path = out_dir / path.name
    manifest_path = out_dir / f"{path.stem}-screening.csv"

    tables = json.loads(path.read_text(encoding="utf-8"))
    kept: list[dict[str, Any]] = []
    summary = SourceSummary("twic", str(path), str(out_path))

    with manifest_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "source_file",
                "ref",
                "event",
                "players",
                "min_rating",
                "max_rating",
                "missing_or_unrated_rows",
                "below_threshold_rows",
                "status",
                "reason",
            ],
        )
        writer.writeheader()
        for table in tables:
            players = table.get("players") or []
            ratings = [parse_int(player.get("rating")) for player in players if isinstance(player, dict)]
            ok, reason, missing, below, min_rating, max_rating = rate_values(ratings, threshold)
            summary.total_events += 1
            summary.total_rows += len(players)
            summary.missing_or_unrated_rows += missing
            summary.below_threshold_rows += below
            if ok:
                kept.append(table)
                summary.kept_events += 1
                summary.kept_rows += len(players)
            else:
                summary.rejected_events += 1
                summary.rejected_rows += len(players)
            writer.writerow(
                {
                    "source_file": path.name,
                    "ref": table.get("ref", ""),
                    "event": table.get("event", ""),
                    "players": len(players),
                    "min_rating": "" if min_rating is None else min_rating,
                    "max_rating": "" if max_rating is None else max_rating,
                    "missing_or_unrated_rows": missing,
                    "below_threshold_rows": below,
                    "status": "keep" if ok else "reject",
                    "reason": reason,
                }
            )

    write_json(out_path, kept)
    summary.notes = f"manifest={manifest_path}"
    return summary


def cull_chessmetrics(events_path: Path, results_path: Path, out_dir: Path, threshold: int) -> SourceSummary | None:
    if not events_path.exists() or not results_path.exists():
        return None

    ensure_dir(out_dir)
    out_events = out_dir / events_path.name
    out_results = out_dir / results_path.name
    manifest_path = out_dir / "chessmetrics-screening.csv"

    per_event: dict[str, dict[str, Any]] = {}
    result_header: list[str]

    with results_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        result_header = reader.fieldnames or []
        for row in reader:
            event_id = row.get("EventID", "")
            rating = parse_int(row.get("Rating"))
            bucket = per_event.setdefault(
                event_id,
                {
                    "event_name": row.get("EventName", ""),
                    "rows": 0,
                    "ratings": [],
                    "result_rows": [],
                },
            )
            bucket["rows"] += 1
            bucket["ratings"].append(rating)
            bucket["result_rows"].append(row)

    decisions: dict[str, tuple[bool, str, int, int, int | None, int | None]] = {}
    for event_id, bucket in per_event.items():
        decisions[event_id] = rate_values(bucket["ratings"], threshold)

    summary = SourceSummary("chessmetrics", str(results_path), f"{out_events}; {out_results}")
    event_header: list[str]
    kept_event_ids: set[str] = set()

    with (
        events_path.open("r", encoding="utf-8-sig", newline="") as in_events,
        out_events.open("w", encoding="utf-8", newline="") as out_events_fh,
        manifest_path.open("w", encoding="utf-8", newline="") as manifest_fh,
    ):
        reader = csv.DictReader(in_events)
        event_header = reader.fieldnames or []
        event_writer = csv.DictWriter(out_events_fh, fieldnames=event_header, lineterminator="\n")
        manifest_writer = csv.DictWriter(
            manifest_fh,
            fieldnames=[
                "event_id",
                "event_name",
                "players",
                "min_rating",
                "max_rating",
                "missing_or_unrated_rows",
                "below_threshold_rows",
                "status",
                "reason",
            ],
            lineterminator="\n",
        )
        event_writer.writeheader()
        manifest_writer.writeheader()

        for event in reader:
            event_id = event.get("EventID", "")
            bucket = per_event.get(event_id, {"ratings": [], "rows": 0})
            ok, reason, missing, below, min_rating, max_rating = decisions.get(
                event_id, rate_values([], threshold)
            )
            summary.total_events += 1
            summary.total_rows += bucket["rows"]
            summary.missing_or_unrated_rows += missing
            summary.below_threshold_rows += below
            if ok:
                kept_event_ids.add(event_id)
                event_writer.writerow(event)
                summary.kept_events += 1
                summary.kept_rows += bucket["rows"]
            else:
                summary.rejected_events += 1
                summary.rejected_rows += bucket["rows"]
            manifest_writer.writerow(
                {
                    "event_id": event_id,
                    "event_name": event.get("EventName", ""),
                    "players": bucket["rows"],
                    "min_rating": "" if min_rating is None else min_rating,
                    "max_rating": "" if max_rating is None else max_rating,
                    "missing_or_unrated_rows": missing,
                    "below_threshold_rows": below,
                    "status": "keep" if ok else "reject",
                    "reason": reason,
                }
            )

    with out_results.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=result_header, lineterminator="\n")
        writer.writeheader()
        with results_path.open("r", encoding="utf-8-sig", newline="") as in_results:
            reader = csv.DictReader(in_results)
            for row in reader:
                if row.get("EventID", "") in kept_event_ids:
                    writer.writerow(row)

    summary.notes = f"events_header={len(event_header)} columns; manifest={manifest_path}"
    return summary


def build_edo_match_player_index(players_dir: Path) -> dict[str, list[str]]:
    """Index Edo match ids to exact player ids via player-page match links."""
    if not players_dir.exists():
        return {}
    pattern = re.compile(r"\.\./matches/(m\d+)\.html")
    index: dict[str, set[str]] = {}
    for path in players_dir.glob("p*.html"):
        player_id = path.stem
        text = path.read_text(encoding="utf-8", errors="replace")
        for match_id in set(pattern.findall(text)):
            index.setdefault(match_id, set()).add(player_id)
    return {match_id: sorted(player_ids) for match_id, player_ids in index.items()}


def cull_edo_db(
    db_path: Path,
    players_dir: Path,
    out_dir: Path,
    threshold: int,
    drop_matches: bool,
) -> SourceSummary | None:
    if not db_path.exists():
        return None

    ensure_dir(out_dir)
    out_path = out_dir / f"{db_path.stem}-strict-{threshold}{db_path.suffix}"
    manifest_path = out_dir / "edo-tournament-screening.csv"
    if out_path.exists():
        out_path.unlink()
    shutil.copy2(db_path, out_path)
    match_manifest_path = out_dir / "edo-match-screening.csv"

    summary = SourceSummary("edo", str(db_path), str(out_path))
    match_index = {} if drop_matches else build_edo_match_player_index(players_dir)
    conn = sqlite3.connect(out_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS strict_keep_tournament")
        conn.execute("DROP TABLE IF EXISTS strict_reject_tournament")
        conn.execute(
            """
            CREATE TEMP TABLE strict_tournament_profile AS
            SELECT
              t.tournament_id,
              t.page_title,
              COUNT(r.row_index) AS rows,
              SUM(CASE WHEN r.edo IS NULL THEN 1 ELSE 0 END) AS missing,
              SUM(CASE WHEN r.edo IS NOT NULL AND r.edo < ? THEN 1 ELSE 0 END) AS below,
              MIN(r.edo) AS min_rating,
              MAX(r.edo) AS max_rating
            FROM tournaments t
            LEFT JOIN tournament_result_rows r
              ON r.tournament_id = t.tournament_id AND r.row_kind = 'score'
            GROUP BY t.tournament_id
            """,
            (threshold,),
        )
        conn.execute(
            """
            CREATE TEMP TABLE strict_keep_tournament AS
            SELECT tournament_id FROM strict_tournament_profile
            WHERE rows > 0 AND missing = 0 AND below = 0
            """
        )
        conn.execute(
            """
            CREATE TEMP TABLE strict_reject_tournament AS
            SELECT tournament_id FROM strict_tournament_profile
            WHERE tournament_id NOT IN (SELECT tournament_id FROM strict_keep_tournament)
            """
        )
        conn.execute("CREATE TEMP TABLE strict_match_player (event_id TEXT NOT NULL, player_id TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO strict_match_player (event_id, player_id) VALUES (?, ?)",
            [
                (match_id, player_id)
                for match_id, player_ids in match_index.items()
                for player_id in player_ids
            ],
        )
        conn.execute(
            """
            CREATE TEMP TABLE strict_match_profile AS
            SELECT
              e.year,
              e.event_id,
              e.event_text,
              COUNT(mp.player_id) AS linked_players,
              SUM(CASE WHEN yr.edo IS NULL THEN 1 ELSE 0 END) AS missing,
              SUM(CASE WHEN yr.edo IS NOT NULL AND yr.edo < ? THEN 1 ELSE 0 END) AS below,
              MIN(yr.edo) AS min_rating,
              MAX(yr.edo) AS max_rating
            FROM year_events e
            LEFT JOIN strict_match_player mp ON mp.event_id = e.event_id
            LEFT JOIN year_ratings yr ON yr.year = e.year AND yr.player_id = mp.player_id
            WHERE e.event_kind = 'match'
            GROUP BY e.year, e.event_id, e.event_text
            """,
            (threshold,),
        )
        conn.execute(
            """
            CREATE TEMP TABLE strict_keep_match AS
            SELECT event_id FROM strict_match_profile
            WHERE linked_players = 2 AND missing = 0 AND below = 0
            """
        )

        profiles = conn.execute(
            """
            SELECT tournament_id, page_title, rows, missing, below, min_rating, max_rating,
                   CASE
                     WHEN rows = 0 THEN 'no score rows'
                     WHEN missing > 0 THEN missing || ' missing/unrated player ratings'
                     WHEN below > 0 THEN below || ' player ratings below threshold'
                     ELSE 'all player ratings >= threshold'
                   END AS reason,
                   CASE
                     WHEN tournament_id IN (SELECT tournament_id FROM strict_keep_tournament)
                     THEN 'keep' ELSE 'reject'
                   END AS status
            FROM strict_tournament_profile
            ORDER BY tournament_id
            """
        ).fetchall()

        with manifest_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(
                [
                    "tournament_id",
                    "page_title",
                    "players",
                    "min_rating",
                    "max_rating",
                    "missing_or_unrated_rows",
                    "below_threshold_rows",
                    "status",
                    "reason",
                ]
            )
            for tournament_id, page_title, rows, missing, below, min_rating, max_rating, reason, status in profiles:
                writer.writerow(
                    [
                        tournament_id,
                        page_title,
                        rows,
                        "" if min_rating is None else min_rating,
                        "" if max_rating is None else max_rating,
                        missing,
                        below,
                        status,
                        reason,
                    ]
                )

        match_profiles = conn.execute(
            """
            SELECT year, event_id, event_text, linked_players, missing, below, min_rating, max_rating,
                   CASE
                     WHEN linked_players = 0 THEN 'no linked player pages'
                     WHEN linked_players < 2 THEN 'fewer than two linked player pages'
                     WHEN linked_players > 2 THEN 'more than two linked player pages'
                     WHEN missing > 0 THEN missing || ' missing/unrated annual Edo ratings'
                     WHEN below > 0 THEN below || ' annual Edo ratings below threshold'
                     ELSE 'both annual Edo ratings >= threshold'
                   END AS reason,
                   CASE
                     WHEN event_id IN (SELECT event_id FROM strict_keep_match)
                     THEN 'keep' ELSE 'reject'
                   END AS status
            FROM strict_match_profile
            ORDER BY year, event_id
            """
        ).fetchall()

        with match_manifest_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(
                [
                    "year",
                    "match_id",
                    "event_text",
                    "linked_players",
                    "min_rating",
                    "max_rating",
                    "missing_or_unrated_rows",
                    "below_threshold_rows",
                    "status",
                    "reason",
                ]
            )
            for year, event_id, event_text, linked_players, missing, below, min_rating, max_rating, reason, status in match_profiles:
                unresolved = max(0, 2 - int(linked_players or 0))
                writer.writerow(
                    [
                        year,
                        event_id,
                        event_text,
                        linked_players,
                        "" if min_rating is None else min_rating,
                        "" if max_rating is None else max_rating,
                        int(missing or 0) + unresolved,
                        below,
                        status,
                        reason,
                    ]
                )

        kept_tournaments = conn.execute("SELECT COUNT(*) FROM strict_keep_tournament").fetchone()[0]
        kept_matches = conn.execute("SELECT COUNT(*) FROM strict_keep_match").fetchone()[0]
        summary.total_events = len(profiles) + len(match_profiles)
        summary.kept_events = kept_tournaments + kept_matches
        summary.rejected_events = summary.total_events - summary.kept_events
        row_counts = conn.execute(
            """
            SELECT
              COUNT(*),
              SUM(CASE WHEN tournament_id IN (SELECT tournament_id FROM strict_keep_tournament) THEN 1 ELSE 0 END),
              SUM(CASE WHEN tournament_id IN (SELECT tournament_id FROM strict_reject_tournament) THEN 1 ELSE 0 END),
              SUM(CASE WHEN edo IS NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN edo IS NOT NULL AND edo < ? THEN 1 ELSE 0 END)
            FROM tournament_result_rows
            WHERE row_kind = 'score'
            """,
            (threshold,),
        ).fetchone()
        match_row_counts = conn.execute(
            """
            SELECT
              COALESCE(SUM(linked_players), 0),
              COALESCE(SUM(CASE WHEN event_id IN (SELECT event_id FROM strict_keep_match) THEN linked_players ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN event_id NOT IN (SELECT event_id FROM strict_keep_match) THEN linked_players ELSE 0 END), 0),
              COALESCE(SUM(missing + CASE WHEN linked_players < 2 THEN 2 - linked_players ELSE 0 END), 0),
              COALESCE(SUM(below), 0)
            FROM strict_match_profile
            """
        ).fetchone()
        summary.total_rows = (row_counts[0] or 0) + (match_row_counts[0] or 0)
        summary.kept_rows = (row_counts[1] or 0) + (match_row_counts[1] or 0)
        summary.rejected_rows = (row_counts[2] or 0) + (match_row_counts[2] or 0)
        summary.missing_or_unrated_rows = (row_counts[3] or 0) + (match_row_counts[3] or 0)
        summary.below_threshold_rows = (row_counts[4] or 0) + (match_row_counts[4] or 0)

        conn.execute(
            """
            CREATE TEMP TABLE strict_keep_player AS
            SELECT DISTINCT player_id FROM tournament_result_rows
            WHERE tournament_id IN (SELECT tournament_id FROM strict_keep_tournament)
              AND row_kind = 'score'
              AND player_id IS NOT NULL
            UNION
            SELECT DISTINCT player_id FROM strict_match_player
            WHERE event_id IN (SELECT event_id FROM strict_keep_match)
            """
        )
        conn.execute(
            """
            CREATE TEMP TABLE strict_keep_location AS
            SELECT DISTINCT place_id AS location_id FROM tournaments
            WHERE tournament_id IN (SELECT tournament_id FROM strict_keep_tournament)
              AND place_id IS NOT NULL
              AND place_id <> ''
            UNION
            SELECT DISTINCT location_id FROM location_events
            WHERE event_kind = 'match'
              AND event_id IN (SELECT event_id FROM strict_keep_match)
            """
        )

        conn.execute("DELETE FROM tournament_result_rows WHERE tournament_id NOT IN (SELECT tournament_id FROM strict_keep_tournament)")
        conn.execute("DELETE FROM tournaments WHERE tournament_id NOT IN (SELECT tournament_id FROM strict_keep_tournament)")
        conn.execute(
            """
            DELETE FROM year_events
            WHERE (event_kind = 'tournament' AND event_id NOT IN (SELECT tournament_id FROM strict_keep_tournament))
               OR (event_kind = 'match' AND event_id NOT IN (SELECT event_id FROM strict_keep_match))
            """
        )
        conn.execute(
            """
            DELETE FROM location_events
            WHERE location_id NOT IN (SELECT location_id FROM strict_keep_location)
               OR (event_kind = 'tournament' AND event_id NOT IN (SELECT tournament_id FROM strict_keep_tournament))
               OR (event_kind = 'match' AND event_id NOT IN (SELECT event_id FROM strict_keep_match))
            """
        )
        conn.execute("DELETE FROM player_ratings WHERE player_id NOT IN (SELECT player_id FROM strict_keep_player)")
        conn.execute("DELETE FROM year_ratings WHERE player_id NOT IN (SELECT player_id FROM strict_keep_player)")
        conn.execute("DELETE FROM players WHERE player_id NOT IN (SELECT player_id FROM strict_keep_player)")
        conn.execute("DELETE FROM locations WHERE location_id NOT IN (SELECT location_id FROM strict_keep_location)")
        conn.execute(
            """
            DELETE FROM registry_entries
            WHERE (kind = 'tournament' AND native_id NOT IN (SELECT tournament_id FROM strict_keep_tournament))
               OR (kind = 'player' AND native_id NOT IN (SELECT player_id FROM strict_keep_player))
               OR (kind = 'location' AND native_id NOT IN (SELECT location_id FROM strict_keep_location))
            """
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    summary.notes = (
        f"manifest={manifest_path}; match_manifest={match_manifest_path}; "
        f"match_player_index={len(match_index)} ids"
    )
    return summary


def write_summary(out_dir: Path, summaries: list[SourceSummary], threshold: int) -> None:
    payload = {
        "threshold": threshold,
        "summaries": [asdict(summary) for summary in summaries],
    }
    write_json(out_dir / "strict-rating-floor-summary.json", payload)

    with (out_dir / "strict-rating-floor-summary.csv").open("w", encoding="utf-8", newline="") as fh:
        fieldnames = list(asdict(SourceSummary("x", "x", "x")).keys())
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for summary in summaries:
            writer.writerow(asdict(summary))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cull local TWIC, Edo, and Chessmetrics staging sources with a strict per-player rating floor."
    )
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--out-dir", type=Path, default=Path(r"D:\ctml\build\staging-strict-2000"))
    parser.add_argument("--twic-json", type=Path, default=Path(r"D:\ctml\build\crosstables\twic.json"))
    parser.add_argument("--twic-pre-json", type=Path, default=Path(r"D:\ctml\build\crosstables\twic-pre.json"))
    parser.add_argument("--edo-db", type=Path, default=Path(r"D:\edo\edo_registry.sqlite"))
    parser.add_argument("--edo-players-dir", type=Path, default=Path(r"D:\edo\players"))
    parser.add_argument("--chessmetrics-events", type=Path, default=Path(r"D:\chessnerd\chessmetrics_events.csv"))
    parser.add_argument("--chessmetrics-results", type=Path, default=Path(r"D:\chessnerd\chessmetrics_event_results.csv"))
    parser.add_argument(
        "--drop-edo-matches",
        action="store_true",
        help="Drop Edo year-list match entries instead of screening them via player-page links and year ratings.",
    )
    parser.add_argument("--keep-edo-matches", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    ensure_dir(args.out_dir)
    summaries: list[SourceSummary] = []

    for twic_path in [args.twic_json, args.twic_pre_json]:
        summary = cull_twic_json(twic_path, args.out_dir / "twic", args.threshold)
        if summary:
            summaries.append(summary)

    edo_summary = cull_edo_db(
        args.edo_db,
        args.edo_players_dir,
        args.out_dir / "edo",
        args.threshold,
        args.drop_edo_matches,
    )
    if edo_summary:
        summaries.append(edo_summary)

    chessmetrics_summary = cull_chessmetrics(
        args.chessmetrics_events,
        args.chessmetrics_results,
        args.out_dir / "chessmetrics",
        args.threshold,
    )
    if chessmetrics_summary:
        summaries.append(chessmetrics_summary)

    write_summary(args.out_dir, summaries, args.threshold)
    for summary in summaries:
        print(
            f"{summary.source}: kept {summary.kept_events}/{summary.total_events} events, "
            f"{summary.kept_rows}/{summary.total_rows} player rows -> {summary.output}"
        )
    print(f"summary: {args.out_dir / 'strict-rating-floor-summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
