#!/usr/bin/env python3
"""Cluster source tournament CTML drafts and emit reviewable merge drafts.

This is a curation aid, not a publication step. It preserves source documents
as-is, groups likely duplicate tournaments, and writes cluster reports plus
merged draft CTML for clusters with multiple source documents.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from readers.ctml_source_common import crosstable_to_ctml, normalize_fed, normalize_space, parse_iso_date, parse_points, to_int, write_json


NS = {"ctml": "urn:ctml:1.0"}
DEFAULT_SOURCES = Path(r"D:\ctml\build\drafts")
DEFAULT_OUT = Path(r"D:\ctml\build\curation")
SOURCE_PRIORITY = {
    "chess-results": 100,
    "twic": 90,
    "edo": 80,
    "nwchess-minev": 70,
    "olimpbase": 60,
}


@dataclass
class Participant:
    ref: str
    name: str
    fed: str
    title: str
    fide_id: str
    rating: int | None
    score: float | None
    rank: int | None


@dataclass
class TournamentFact:
    path: Path
    tid: str
    source: str
    source_uri: str
    name: str
    event_ref: str
    start: dict[str, int | str | None]
    end: dict[str, int | str | None]
    place: str
    participants: list[Participant]

    @property
    def year(self) -> int:
        return int(self.start["y"] or 0)

    @property
    def month(self) -> int:
        return int(self.start["m"] or 0)

    @property
    def roster_keys(self) -> set[str]:
        out = set()
        for p in self.participants:
            if p.ref:
                out.add(p.ref)
            elif p.name:
                out.add(f"{name_key(p.name)}|{p.fed}")
        return out


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def name_key(value: str) -> str:
    text = normalize_space(value).lower().replace("&", " and ")
    text = re.sub(r"^\d{1,3}(st|nd|rd|th)\s+", "", text)
    text = re.sub(r"\b(1[5-9]\d{2}|20\d{2})\b", "", text)
    text = re.sub(r"\b(open|tournament|championship|final|group|section)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    aa, bb = set(ka.split()), set(kb.split())
    token = len(aa & bb) / len(aa | bb) if aa and bb else 0.0
    return 0.65 * SequenceMatcher(None, ka, kb).ratio() + 0.35 * token


def date_attrs(elem: ET.Element | None) -> dict[str, int | str | None]:
    if elem is None:
        return {"y": None, "m": None, "d": None, "precision": None, "iso": None}
    return {
        "y": int(elem.attrib["y"]) if elem.attrib.get("y") else None,
        "m": int(elem.attrib["m"]) if elem.attrib.get("m") else None,
        "d": int(elem.attrib["d"]) if elem.attrib.get("d") else None,
        "precision": elem.attrib.get("precision"),
        "iso": elem.attrib.get("iso"),
    }


def date_text(d: dict[str, int | str | None]) -> str:
    y = d.get("y")
    if not y:
        return ""
    if d.get("m") and d.get("d"):
        return f"{int(y):04}-{int(d['m']):02}-{int(d['d']):02}"
    if d.get("m"):
        return f"{int(y):04}-{int(d['m']):02}"
    return str(y)


def child_text(elem: ET.Element | None, path: str) -> str:
    return normalize_space(elem.findtext(path, default="", namespaces=NS) if elem is not None else "")


def parse_participant(elem: ET.Element) -> Participant | None:
    pref = elem.find("ctml:playerRef", NS)
    if pref is None:
        return None
    name_elem = pref.find("ctml:name", NS)
    name = normalize_space(name_elem.attrib.get("display", "") if name_elem is not None else "")
    if not name and name_elem is not None:
        name = normalize_space(" ".join(name_elem.itertext()))
    ids = pref.find("ctml:ids", NS)
    fide_id = child_text(ids, "ctml:fideId")
    note = child_text(elem, "ctml:notes")
    rank = None
    m = re.search(r"rank=(\d+)", note)
    if m:
        rank = int(m.group(1))
    rating_elem = elem.find("ctml:ratingSnapshot/ctml:value", NS)
    rating = to_int(rating_elem.text if rating_elem is not None else "")
    score = parse_points(child_text(elem, "ctml:score"))
    return Participant(
        ref=pref.attrib.get("ref", ""),
        name=name,
        fed=normalize_fed(child_text(pref, "ctml:federation")),
        title=child_text(pref, "ctml:title"),
        fide_id=fide_id,
        rating=rating,
        score=score,
        rank=rank,
    )


def infer_source(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    return rel.parts[0] if rel.parts else ""


def parse_tournament(path: Path, root: Path) -> TournamentFact | None:
    try:
        doc = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    if doc.tag != f"{{{NS['ctml']}}}tournament":
        return None
    header = doc.find("ctml:header", NS)
    dates = header.find("ctml:dates", NS) if header is not None else None
    start = date_attrs(dates.find("ctml:start", NS) if dates is not None else None)
    if not start.get("y"):
        return None
    place_ref = header.find("ctml:placeRef", NS) if header is not None else None
    participants = []
    for elem in doc.findall("ctml:participants/ctml:participant", NS):
        parsed = parse_participant(elem)
        if parsed:
            participants.append(parsed)
    source_elem = doc.find("ctml:source", NS)
    source = source_elem.attrib.get("kind", "") if source_elem is not None else infer_source(path, root)
    return TournamentFact(
        path=path,
        tid=doc.attrib.get("id", path.stem),
        source=source or infer_source(path, root),
        source_uri=child_text(source_elem, "ctml:uri"),
        name=child_text(header, "ctml:name"),
        event_ref=(header.find("ctml:eventRef", NS).attrib.get("ref", "") if header is not None and header.find("ctml:eventRef", NS) is not None else ""),
        start=start,
        end=date_attrs(dates.find("ctml:end", NS) if dates is not None else None),
        place=child_text(place_ref, "ctml:name"),
        participants=participants,
    )


def fact_from_json_table(table: dict[str, Any], json_path: Path, index: int) -> TournamentFact | None:
    start = parse_iso_date(table.get("start"))
    if start is None:
        return None
    end = parse_iso_date(table.get("end")) or start
    participants = []
    for player in table.get("players", []):
        name = normalize_space(player.get("name"))
        if not name:
            continue
        fide_id = normalize_space(player.get("fide_id"))
        ref = normalize_space(player.get("ref"))
        if not ref and re.fullmatch(r"\d{4,12}", fide_id):
            ref = f"player:fide:{fide_id}"
        participants.append(
            Participant(
                ref=ref,
                name=name,
                fed=normalize_fed(player.get("fed")),
                title=normalize_space(player.get("title")),
                fide_id=fide_id,
                rating=to_int(player.get("rating")),
                score=parse_points(player.get("score")),
                rank=to_int(player.get("rank")),
            )
        )
    if not participants:
        return None
    source = normalize_space(table.get("source")) or json_path.parent.name
    return TournamentFact(
        path=Path(f"{json_path}#{index:06d}"),
        tid=normalize_space(table.get("ref")) or f"{json_path.stem}-{index}",
        source=source,
        source_uri=normalize_space(table.get("url")),
        name=normalize_space(table.get("event")),
        event_ref=normalize_space(table.get("event_ref")),
        start={"y": start.y, "m": start.m, "d": start.d, "precision": start.precision, "iso": None},
        end={"y": end.y, "m": end.m, "d": end.d, "precision": end.precision, "iso": None},
        place=normalize_space(table.get("place")),
        participants=participants,
    )


def collect_json_facts(root: Path) -> list[TournamentFact]:
    json_paths = [
        root / "chess-results" / "chess-results-crosstables.json",
        root / "twic" / "twic-crosstables.json",
        root / "nwchess-minev" / "nwchess-minev-crosstables.json",
        root / "olimpbase" / "olimpbase-crosstables.json",
    ]
    facts: list[TournamentFact] = []
    for json_path in json_paths:
        if not json_path.exists():
            continue
        with json_path.open(encoding="utf-8") as handle:
            tables = json.load(handle)
        for index, table in enumerate(tables, start=1):
            fact = fact_from_json_table(table, json_path, index)
            if fact is not None:
                facts.append(fact)
    return facts


def collect_xml_facts(root: Path) -> list[TournamentFact]:
    facts = []
    # Edo has no crosstable-contract JSON intermediate; parse those CTML drafts.
    edo = root / "edo" / "tournaments"
    if edo.exists():
        for path in sorted(edo.glob("*.xml")):
            fact = parse_tournament(path, root)
            if fact is not None:
                facts.append(fact)
    return facts


def collect_facts(root: Path) -> list[TournamentFact]:
    return collect_json_facts(root) + collect_xml_facts(root)


def should_link(a: TournamentFact, b: TournamentFact) -> bool:
    if a.event_ref and a.event_ref == b.event_ref:
        return True
    if a.event_ref or b.event_ref:
        return False
    if a.year != b.year:
        return False
    if a.month and b.month and abs(a.month - b.month) > 1:
        return False
    sim = similarity(a.name, b.name)
    if sim >= 0.94:
        return True
    ar, br = a.roster_keys, b.roster_keys
    if ar and br:
        overlap = len(ar & br) / min(len(ar), len(br))
        if overlap >= 0.5 and sim >= 0.62:
            return True
    return False


def cluster_facts(facts: list[TournamentFact]) -> list[list[int]]:
    uf = UnionFind(len(facts))
    by_event_ref: dict[str, list[int]] = {}
    blocks: dict[tuple[int, str], list[int]] = {}
    for idx, fact in enumerate(facts):
        if fact.event_ref:
            by_event_ref.setdefault(fact.event_ref, []).append(idx)
        key = name_key(fact.name)
        token = key.split()[0] if key.split() else ""
        blocks.setdefault((fact.year, token), []).append(idx)
    for indexes in by_event_ref.values():
        for idx in indexes[1:]:
            uf.union(indexes[0], idx)
    for indexes in blocks.values():
        if len(indexes) > 400:
            continue
        for pos, left in enumerate(indexes):
            for right in indexes[pos + 1 :]:
                if should_link(facts[left], facts[right]):
                    uf.union(left, right)
    clusters: dict[int, list[int]] = {}
    for idx in range(len(facts)):
        clusters.setdefault(uf.find(idx), []).append(idx)
    return sorted(clusters.values(), key=lambda c: (-len(c), facts[c[0]].year, facts[c[0]].name))


def best_fact(cluster: list[TournamentFact]) -> TournamentFact:
    return max(
        cluster,
        key=lambda f: (
            SOURCE_PRIORITY.get(f.source, 0),
            len(f.participants),
            sum(1 for p in f.participants if p.fide_id or p.ref.startswith("player:fide:")),
            sum(1 for p in f.participants if p.rating is not None),
        ),
    )


def merged_table(cluster_id: str, cluster: list[TournamentFact]) -> dict[str, Any]:
    best = best_fact(cluster)
    participants: dict[str, Participant] = {}
    for fact in sorted(cluster, key=lambda f: SOURCE_PRIORITY.get(f.source, 0), reverse=True):
        for p in fact.participants:
            key = p.ref or f"{name_key(p.name)}|{p.fed}"
            old = participants.get(key)
            if old is None:
                participants[key] = p
                continue
            participants[key] = Participant(
                ref=old.ref or p.ref,
                name=old.name or p.name,
                fed=old.fed or p.fed,
                title=old.title or p.title,
                fide_id=old.fide_id or p.fide_id,
                rating=old.rating if old.rating is not None else p.rating,
                score=old.score if old.score is not None else p.score,
                rank=old.rank if old.rank is not None else p.rank,
            )
    players = [
        {
            "rank": p.rank,
            "ref": p.ref,
            "name": p.name,
            "fed": p.fed,
            "title": p.title,
            "fide_id": p.fide_id,
            "rating": p.rating,
            "score": p.score,
        }
        for p in participants.values()
    ]
    players.sort(key=lambda p: (p["rank"] is None, p["rank"] or 9999, p["name"]))
    return {
        "source": "curation-draft",
        "reader": "curate_source_tournaments/0.1",
        "ref": cluster_id,
        "event": best.name,
        "event_ref": best.event_ref,
        "place": best.place,
        "start": date_text(best.start),
        "end": date_text(best.end),
        "format": "unknown",
        "rating_system": "unknown",
        "url": best.source_uri,
        "notes": "Review draft merged from source tournaments: "
        + "; ".join(f"{f.source}:{f.path.name}" for f in cluster),
        "players": players,
    }


def fact_summary(fact: TournamentFact, root: Path) -> dict[str, Any]:
    return {
        "path": str(fact.path),
        "relative_path": str(fact.path.relative_to(root)) if fact.path.is_absolute() and fact.path.is_relative_to(root) else str(fact.path),
        "id": fact.tid,
        "source": fact.source,
        "name": fact.name,
        "event_ref": fact.event_ref,
        "start": date_text(fact.start),
        "end": date_text(fact.end),
        "place": fact.place,
        "participant_count": len(fact.participants),
        "source_uri": fact.source_uri,
    }


def write_outputs(root: Path, out_dir: Path, facts: list[TournamentFact], clusters: list[list[int]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "source-tournament-index.jsonl"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        for fact in facts:
            handle.write(json.dumps(fact_summary(fact, root), ensure_ascii=False) + "\n")

    rows = []
    duplicate_rows = []
    merged_dir = out_dir / "merged-drafts"
    merged_dir.mkdir(parents=True, exist_ok=True)
    for old in merged_dir.glob("*.xml"):
        old.unlink()
    for number, indexes in enumerate(clusters, start=1):
        cluster = [facts[i] for i in indexes]
        cid = f"cluster-{number:06d}"
        best = best_fact(cluster)
        row = {
            "cluster_id": cid,
            "size": len(cluster),
            "sources": ",".join(sorted({f.source for f in cluster})),
            "canonical_name": best.name,
            "start": date_text(best.start),
            "end": date_text(best.end),
            "place": best.place,
            "participant_counts": ",".join(str(len(f.participants)) for f in cluster),
            "event_refs": ",".join(sorted({f.event_ref for f in cluster if f.event_ref})),
            "best_doc": str(best.path),
            "members": " | ".join(str(f.path) for f in cluster),
        }
        rows.append(row)
        if len(cluster) > 1:
            duplicate_rows.append(row)
            xml = crosstable_to_ctml(merged_table(cid, cluster))
            if xml:
                (merged_dir / f"{cid}.xml").write_text(xml, encoding="utf-8")
    for path, data in (
        (out_dir / "source-tournament-clusters.tsv", rows),
        (out_dir / "source-tournament-duplicates.tsv", duplicate_rows),
    ):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [], delimiter="\t")
            writer.writeheader()
            writer.writerows(data)
    summary = {
        "source_documents": len(facts),
        "clusters": len(clusters),
        "duplicate_clusters": len(duplicate_rows),
        "merged_drafts": len(list(merged_dir.glob("*.xml"))),
    }
    write_json(summary, out_dir / "summary.json")
    print(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default=str(DEFAULT_SOURCES))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    root = Path(args.sources)
    facts = collect_facts(root)
    clusters = cluster_facts(facts)
    write_outputs(root, Path(args.out), facts, clusters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
