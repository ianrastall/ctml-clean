# Changelog

Schema-only changelog for this repository. The pipeline project
(`D:\dev\proj\ctml`) has its own `docs/HANDOFF.md` recording the authoring
history up to and including the design that this repository's `xsd/` was
migrated from on 2026-08-05; entries below start from that migration.

## 2026-08-05

**This repository (`ctml-clean`) becomes the canonical, version-controlled
home for the CTML schema.** The `xsd/` directory (10 files, the v2 module
set: vocab, names, dates, entities, places, events, analysis, game, core,
plus the `ctml.xsd` umbrella) was moved here from `D:\dev\proj\ctml\xsd`,
which had never been under version control. `assets/all.tsv` (a
lichess-style ECO opening table: code, name, PGN, UCI, EPD) was added
alongside it for a planned opening-classification tool.

Changes made to the schema in this session, each following from a
project-owner decision recorded at the time:

- **Namespace bumped `urn:ctml:1.0` -> `urn:ctml:2.0`.** Every module's
  documentation already claimed "CTML 2.0"; the namespace URI was the one
  place that still said `1.0`. Chosen over the zero-migration alternative
  (leave the namespace alone, just fix the wording) specifically because it
  requires the namespace to actually match what the docs say, at the cost of
  a one-time migration of every already-emitted document. That migration was
  performed the same session against the full `D:\dev\proj\ctml\registry`
  and `samples` trees (255 files: event registry, player registry sharded
  A–Z/OTHER, place registry sharded by country, plus the four hand-written
  samples) and against the `CTML_NS` constant duplicated in nine pipeline
  scripts under `readers/` and `scripts/`. See that project's own
  `docs/HANDOFF.md` for the migration record. `@ctmlVersion="2.0"` was
  already the value every emitter wrote, so no change was needed there.
- **Non-game representation added** (`ctml-vocab.xsd`: `NonGameKindType`;
  `ctml-core.xsd`: `NonGameType`, `NonGameSetType`, `GamesType/nonGames`,
  `ParticipantType/withdrawnAfterRound`). Closes a real gap: crosstables,
  especially Swiss ones, routinely have round-participant slots that are not
  a played game between two known opponents — byes (full/half/zero-point),
  single-sided forfeits, and mid-event withdrawals. `ctml:game` cannot
  represent these (`@white`/`@black` are both required `xs:IDREF`s), and
  before this addition they had no representation at all beyond silent
  absence from `games`, indistinguishable from "unknown." A **scheduled**
  pairing where neither side played (a "double forfeit") is deliberately
  *not* part of this addition — it already had a home as an ordinary
  `ctml:game` with `result="0-0" termination="forfeit"`, both of which
  predate this session, so that case needed no new vocabulary.
- **`ctml-crosstable.xsd` (a separate crosstable-registry module) was
  considered and rejected**, not added. Reading the pipeline project's
  actual `crosstable_to_ctml()` (`readers/ctml_source_common.py`) showed a
  crosstable already lands as an ordinary `ctml:tournament` with
  `participants` (roster, rating, score) — no new registry module is
  needed to hold crosstable data; the representational gap was narrower
  than it first looked, and turned out to be exactly the non-game gap
  above, not a missing registry.
- **`EcoCodeType` (bare `[A-E][0-9]{2}`) left unchanged.** A proposal to
  widen it for sub-variation notation (e.g. `B90/12`) was considered and
  dropped: `assets/all.tsv`, the opening table this repository now carries
  for ECO derivation, uses bare codes with the sub-variation distinction
  carried in its `name` column instead, so the existing pattern already
  matches the data it will actually be matched against.
- **`ctml-game.xsd`'s `TagSetType` left as an open-ended token list**, not
  narrowed to a fixed curation-label enum. Nothing in the pipeline currently
  reads or writes it; narrowing it now would be speculative.
- Added `spec/fingerprint.md` (the `zobrist-polyglot-1` scheme, pinned
  byte-exact, with test vectors computed against the actual
  `python-chess 1.11.2` installed on this machine) and `spec/ctml-rules.md`
  (every cross-field rule XSD 1.0 cannot express, consolidated from where
  each was previously only noted inline at its point of relevance).
