r"""Screen the consolidated crosstable dataset with the strict rating floor.

Ported from D:\ctml\scripts\screen_crosstables.py. Schema-agnostic -- this
script only reads the crosstable JSON contract (ratings/player counts) and
never touches CTML XML, so nothing here changed for CTML 2.0.

Corpus admission rule (docs/corpus-policy.md): a tournament is admitted only
if EVERY player on its crosstable has a known rating at or above the floor
(default 2000). A single player below the floor -- or a single player with
no rating at all -- discards the whole tournament.

Usage:
    python screen_crosstables.py [--threshold 2000]

Reads crosstables\crosstables.json (override with --in), writes the
admitted subset to build\crosstables-strict<threshold>.json and a
per-tournament decision manifest to build\screening-strict<threshold>.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = PROJECT_ROOT / "crosstables" / "crosstables.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "build"


def screen(table: dict, threshold: int) -> tuple[bool, str, int | None]:
    players = table.get("players") or []
    if len(players) < 2:
        return False, "fewer than 2 players", None
    ratings = [p.get("rating") for p in players]
    numeric = [r for r in ratings if isinstance(r, int)]
    low = min(numeric) if numeric else None
    missing = len(ratings) - len(numeric)
    if missing:
        return False, f"{missing} of {len(ratings)} players unrated", low
    below = sum(1 for r in numeric if r < threshold)
    if below:
        return False, f"{below} of {len(ratings)} players below {threshold}", low
    return True, f"all {len(ratings)} players rated {threshold}+", low


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    with Path(args.inp).open(encoding="utf-8") as handle:
        tables = json.load(handle)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"crosstables-strict{args.threshold}.json"
    out_csv = out_dir / f"screening-strict{args.threshold}.csv"

    kept: list[dict] = []
    kept_by_source: Counter[str] = Counter()
    total_by_source: Counter[str] = Counter()
    with out_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "ref", "event", "start", "players", "min_rating", "status", "reason"])
        for table in tables:
            source = table.get("source", "?")
            total_by_source[source] += 1
            ok, reason, low = screen(table, args.threshold)
            if ok:
                kept.append(table)
                kept_by_source[source] += 1
            writer.writerow(
                [
                    source,
                    table.get("ref", ""),
                    table.get("event", ""),
                    table.get("start", ""),
                    len(table.get("players") or []),
                    "" if low is None else low,
                    "keep" if ok else "reject",
                    reason,
                ]
            )

    with out_json.open("w", encoding="utf-8") as handle:
        json.dump(kept, handle, ensure_ascii=False, indent=1)

    print(f"admitted {len(kept)} of {len(tables)} tournaments (floor {args.threshold})", file=sys.stderr)
    for source in sorted(total_by_source):
        print(f"  {source}: {kept_by_source[source]} / {total_by_source[source]}", file=sys.stderr)
    print(f"wrote {out_json}\nwrote {out_csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
