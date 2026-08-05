# CTML Handoff

Last updated: 2026-08-05
Status: the Rust program's purpose is DECIDED (see below), and the repo is
now actually on GitHub (`github.com/ianrastall/ctml-clean`, pushed by the
project owner). Seven real commands exist: `ctml-clean stats`,
`ctml-clean diff-players` (exact 529,403/529,403 field parity against the
XML player registry), `ctml-clean ingest-crosstables` (crosstables.json →
ctml:tournament, full corpus schema-validated), `ctml-clean
fingerprint-selftest` (the `zobrist-polyglot-1` scheme, 10/10 spec test
vectors passing), `ctml-clean movegen-selftest`/`perft` (legal move
generator, 27/27 perft cases exact against `python-chess`), and
`ctml-clean pgn-import`/`pgn-selftest` (PGN → SAN-resolved UCI moves +
fingerprints, 65/65 self-test games exact, then run against the real
1.88M-game `mega-database-2025-filtered.pgn` — see its section below for
the result). `.gitignore` excludes the SSP/crosstables.json/
player-registry shards (multiple blow GitHub's 100MB push limit) — see
"Repo size" below. Next: SAN parsing + PGN tag extraction on top of the
move generator built this session (that generator is a building block for
PGN import, not the whole of it — see "Next real unit of work"), then
cross-source tournament dedup.

## Honesty markers

- **DECIDED** — settled by the project owner. Don't relitigate.
- **VERIFIED** — checked directly this session; how is stated.
- **UNKNOWN** — not established. Don't guess, don't fill in.

## Locations on disk (there are four; two are alive)

| Path | Git? | State |
|---|---|---|
| `D:\dev\proj\ctml-clean` | Yes, 0 commits | **Alive. This file's repo.** Canonical, version-controlled CTML schema. |
| `D:\dev\proj\ctml` | No | **Alive.** Python pipeline: crosstable ingest, player/place/event registries (4.7GB real data), PGN import, Zobrist fingerprinting, dedup. Has its own `docs/HANDOFF.md`. No version control at all — a real, standing risk, independent of anything below. |
| `D:\ctml` | Yes | Superseded. Earlier "consolidated from a four-repo split" incarnation; `D:\dev\proj\ctml`'s own HANDOFF documents picking up from it and archiving its Rust workspace as reference-only. |
| `D:\pgn-paladin` | Yes, 0 commits | Dead. VERIFIED by direct inspection: `.gitignore`, a template `Cargo.toml` (`name = "pgn-paladin"`), and a `Hello, world!` `src/main.rs`. Nothing else. This is the repo the old "PGN Paladin" planning document (a separate design conversation, not primary source) described — the design was never built there. Superseded by this repo. |

## What this repository (`ctml-clean`) is, concretely

- `xsd/` — the 10-module CTML v2 schema (vocab, names, dates, entities,
  places, events, analysis, game, core, `ctml.xsd` umbrella). Moved here
  2026-08-05 from `D:\dev\proj\ctml\xsd` specifically to get it under
  version control for the first time ever.
- `assets/all.tsv` — a lichess-style ECO opening table (code, name, pgn,
  uci, epd), dropped in already, unused by any code yet.
- `spec/fingerprint.md`, `spec/ctml-rules.md` — written this session.
  Byte-exact dedup-hash spec with real test vectors, and a checklist of
  every cross-field rule XSD 1.0 can't express.
- `CHANGELOG.md` — this session's schema changes, with reasoning.
- `Cargo.toml` / `src/main.rs` — an untouched `cargo new` template
  (`println!("Hello, world!")`, zero dependencies). **What this is for is
  not decided.** See the last section.
- Nothing in this repo is committed to git yet.

## What changed this session (2026-08-05), VERIFIED

1. **Namespace bumped `urn:ctml:1.0` -> `urn:ctml:2.0`** across all 10
   `xsd/` files, matching what every module's own documentation already
   claimed ("CTML 2.0"). `@ctmlVersion` was already written as `"2.0"`
   everywhere, so that needed no change.
2. **Migrated real data to match**: all 255 XML files under
   `D:\dev\proj\ctml\registry` and `...\samples` (events registry, 27
   player shards, 220+ place shards, 4.7GB total, up to 547MB in one file)
   rewritten in place (atomic per-file: temp file + verify + rename), each
   checked for exactly one namespace occurrence before touching it. Zero
   files needed manual attention.
3. **Updated the 9 Python files** under `D:\dev\proj\ctml\readers` and
   `...\scripts` that each duplicate a `CTML_NS`/`NS` constant, so future
   pipeline runs emit the new namespace.
4. **Added non-game representation** to `ctml-vocab.xsd`
   (`NonGameKindType`) and `ctml-core.xsd` (`NonGameType`,
   `NonGameSetType`, `GamesType/nonGames`,
   `ParticipantType/withdrawnAfterRound`) — byes, single-sided forfeits,
   and withdrawals now have a schema home; a scheduled pairing where
   neither side played was already representable as an ordinary
   `ctml:game` with `result="0-0" termination="forfeit"` and needed no
   change.
5. **Declined to add** a separate crosstable-registry module (crosstable
   data already lands as an ordinary `ctml:tournament` via the pipeline's
   existing `crosstable_to_ctml()` — confirmed by reading that function
   directly) and declined to widen `EcoCodeType` for sub-variation
   notation (`assets/all.tsv` uses bare codes; the fuller classification
   lives in its `name` column instead).

Full reasoning for each item is in `CHANGELOG.md`.

## Verification performed, VERIFIED

- `lxml.etree.XMLSchema` compiles the assembled `ctml.xsd` cleanly.
- All 4 hand-written samples, the full `events.xml`, two place shards, and
  both the smallest and largest (547MB) player shards validate clean
  against the updated schema, post-migration.
- A synthetic document exercising the new `nonGame` and
  `withdrawnAfterRound` elements validates correctly.

Python used for all of the above: `D:\dev\lang\python\runtime\python.exe`
(has `lxml` and `python-chess` installed; not on default `PATH`).

## What the Rust program is for — DECIDED

Stated directly by the project owner this session, in prose, unprompted by
a menu: **the Rust program is the fast, industrial-strength version of the
Python pipeline** — the same jobs (crosstable ingest, player/site/event
registries, PGN import, Zobrist fingerprinting, dedup), at C-like speed.
Not a side-utility, not a schema-validator CLI. This settles the
language-split question the previous session left open: Rust is meant to
eventually *replace* the Python pipeline's runtime role, working from the
same CTML v2.0 schema.

## The four real data sources — VERIFIED this session, all under `assets/`

The project owner listed these from memory as "what I can provide before
we even start anything." Each was opened and inspected directly (not
taken on faith) before writing anything against it:

| Path | What it is | VERIFIED shape |
|---|---|---|
| `assets/ratings260801.ssp` | **Source of truth** for players: ratings, bio, aliases. Custom line format, 493MB / 10,078,435 lines. Also carries `@EVENT`/`@SITE`/`@ROUND` sections — these are name-*normalization rule tables* (`%Prefix`/`%Infix`/`%Suffix`), not per-event or per-site records; don't expect event/site data to live here. |
| `assets/registries/events.xml`, `assets/registries/players/players-*.xml` (27 shards), `assets/registries/places/places-*.xml` (223 shards) | The CTML v2.0 XML registries, generated *from* the SSP (and a `dr5hn/countries-states-cities-database` import for places). Already namespaced `urn:ctml:2.0` — the "may be wired for the wrong setup" worry was checked directly and did not hold; no re-migration needed. |
| `assets/all.tsv` | ECO opening table (code, name, PGN, UCI, EPD), lichess-style, 3,811 rows, 788KB. Already noted in `CHANGELOG.md` from the schema session. |
| `assets/crosstables.json` | Scraped tournament crosstables: 48,198 entries (`swiss`/`round-robin`/`match`/`team`/`unknown`), each with a `players[]` array (rank, name, title, fed, rating, score). Raw ingest material — nothing here is CTML-shaped yet; no game-level (move) data in this file, only final standings. |

Registry-vs-source cross-check (computed, not assumed): the XML player
registry holds 617,358 `ctml:player` records; the SSP has 617,361
unindented player-name lines in its `@PLAYER` section. A 3-record gap,
direction and cause not investigated — small enough to be an edge case in
either the SSP block-boundary parsing or the registry generation, not
obviously a sync problem. Worth a look before the two are ever assumed
interchangeable, not before then.

## What this session did

Built the first real command, `ctml-clean stats [assets-dir]`
(`src/main.rs` + `src/{eco,ssp,registry,crosstable}.rs`, deps:
`quick-xml`, `serde`, `serde_json`). It streams (not DOMs) every source
above — important because the players registry alone is 4.6GB — and
prints counts and per-stage timings. Run against the full real data at
`assets/`, not a subset:

```
eco table        assets\all.tsv                            3811 rows
event registry    assets\registries/events.xml              10069 eventSeries
player registry   assets\registries/players                 617358 players, 2103293 aliases, 27 shards
place registry    assets\registries/places                  155073 places, 223 shards
crosstables       assets\crosstables.json                   48198 tournaments, 978924 player-rows
ssp master        assets\ratings260801.ssp                  617361 player records
```

The crosstable count (48,198) was independently cross-checked with a raw
`json.load()` in Python — matched exactly, which is the closest thing to
an independent correctness check available for that parser so far.

**Performance, honestly reported, not glossed over:** the 4.6GB player
registry scan took 58.9s cold (first disk touch) and 11.5s warm (OS page
cache) — roughly 400MB/s warm, single-threaded, doing nothing but counting
`Start`/`Empty` events by local name. That is disk/parse-bound, not yet
"C-like speed" by the standard this program is supposed to meet; it's
adequate for a counting pass but should be revisited (rayon-parallelized
per-shard, since the 27 files are independent, is the obvious first lever)
once real per-record work — not just counting — is added on top of it, so
the optimization target is the real workload rather than a guess.

**Also fixed:** the actual cause of "the executable only prints `Hello,
world!`" — the project owner builds with `cargo build --release
--target-dir build` (this repo's `build/` directory), which does *not* go
through this machine's global `CARGO_TARGET_DIR` env var
(`D:\dev\lang\rust\target`). The stale template binary sitting in `build/`
was simply never rebuilt since `cargo new`. Rebuilt now; `.gitignore` gained
`/build` alongside the pre-existing `/target` (it covered the default
location only, not the one actually in use).

## `ctml-clean diff-players` — typed SSP↔registry diff, this session

Built `src/ssp.rs::parse_players`, `src/xmlplayers.rs` (a manual
stack-based streaming XML parser — plain `serde` XML support doesn't
disambiguate elements like `family`/`given` or `year` that repeat with
different meaning at different nesting depths), and `src/diff.rs`, wired
up as `ctml-clean diff-players [assets-dir]`. Joins on FIDE id (the only
reliable key both sides carry) and compares every player both sides agree
exists — 529,403 of them, not a sample. 87,954 SSP players and 87,955 XML
players have no FIDE id and are outside this diff's join key entirely
(not silently included, not silently dropped — counted and reported as
out of scope).

**Two real bugs, found by the diff and fixed, VERIFIED by rerunning:**

1. **Given-name splitting was wrong for the comma form.** `"Family,
   Given"` kept everything after the comma as one given-name string;
   the registry actually splits it further on whitespace into one
   `<given>` per word, same as the no-comma form. Passed the one hand
   example checked while writing it (`"A B M Jobair, Hossain"` — single
   word after the comma, so the bug didn't show), then showed up as
   81,228/529,403 (15%) `given` mismatches on the full-data run — e.g.
   FIDE id 39909832, `"Eksarevskiy, Albert Alex"`, registry splits to
   `["Albert","Alex"]`. Fixed in `ssp.rs::split_name`; down to 1,120
   (0.2%) after the fix (residual characterized below, not resolved).
2. **Rating-history parsing matched the wrong parent tag.** Checked
   `month`'s parent as `history` instead of `year` (structurally it's
   `history > year > month`), so `cur_history_month` was never set and
   every parsed history came out empty — 529,403/529,403 (100%)
   `rating history` mismatches on the first run, which is itself the
   signature of a parser bug rather than a real disagreement (100%
   is not a plausible data finding). Fixed in
   `xmlplayers.rs::FileParser::enter`/`text`; 0 mismatches after.

**Two real facts about the data, discovered by the diff, not assumed —
this session's `current_rating` field was simply wrong before this and is
now fixed to match:**

3. **The registry's `<current>` rating is the last non-`?` month in the
   `%Elo` history, not the `[NNNN]` bracket token on the name line.**
   These disagree for a large fraction of players — 368,101/529,403
   (70%!) on the first run, comparing bracket-vs-`<current>`. Verified
   directly against three hand-checked players (FIDE 13326520, 16234243,
   24113670): each time, the bracket value was stale and the registry's
   `<current>` exactly matched that player's own last recorded month.
   `ssp.rs` now exposes both: `SspPlayer::bracket_rating` (the raw token,
   kept because it's real data) and `ssp::latest_rating()` (the derived
   value, which is what `diff.rs` now actually compares). 0 mismatches
   after the fix.
4. **Bare `#W` in the SSP is not a FIDE title — it's a gender flag on an
   otherwise untitled player.** `#W` alone occurs 49,032 times and the
   registry never emits a `<title>` for any of them. This session
   initially treated it as "no title" by inference; the project owner
   then moved `scripts/` (the actual generator scripts, previously
   untracked and unfound) into this repo, and reading
   `scripts/ssp_to_ctml_players.py` directly confirmed and precisely
   specified the real rule — see below.

**Read the actual generator script (`scripts/ssp_to_ctml_players.py`,
moved into this repo by the project owner this session) rather than keep
inferring rules from output pairs — closed out every remaining residual
with certainty:**

- **Title** is `token.split('+')`; each sub-token starting with `"W"`
  sets `female=true` regardless of recognition; a sub-token is kept as a
  `<title>` only if it's exactly in `{GM,IM,FM,CM,WGM,WIM,WFM,WCM,NM}`
  (so `WC`/`WF`/`HM`/`WH` are legitimately dropped, not a bug); titles
  are deduplicated in encounter order. `title` is `maxOccurs="unbounded"`
  in `ctml-entities.xsd` specifically for this (`IM+WGM` → two `<title>`
  elements) — this parser's `XmlPlayer`/`SspPlayer` were storing a single
  `Option<String>` and silently keeping only the last one; fixed to
  `Vec<String>`.
- **Sex**: `<ctml:sex>F</ctml:sex>` written whenever `female` above is
  true. Not tracked by this parser at all before this pass; added.
- **Suffix**: `person_name_xml`, comma form only — if the last
  whitespace token after the comma case-insensitively matches
  `{jr, jr., sr, sr., i, ii, iii, iv, v, vi, 2nd, 3rd}`, it's popped into
  a separate `<ctml:suffix>`, not treated as a given name. This is why
  `"Varshini, V"` has no given name in the registry: `V` reads as suffix
  "5th" (Roman numeral), not an initial — this session's own diff output
  had misread it as "the registry drops bare initials", which the alias
  case `"A B M Jobair, H"` (kept, because `h` isn't in the suffix set)
  already contradicted; the script resolves the contradiction exactly.
  The no-comma branch never extracts a suffix — a real asymmetry in the
  generator, kept as-is rather than "fixed" to be symmetric.
- **FIDE id validity**: `VALID_FIDE_ID_RE` requires 4–12 digits
  (`ctml:FideIdType`, `ctml-vocab.xsd`); shorter/longer values are routed
  to `internalId` instead and never become the join key. This file has
  exactly one such record, `%Bio FIDE 19` — this parser accepted it as a
  real FIDE id (matching `player:fide:19`, which doesn't exist in the
  registry) until this pass added the same length check.

After all of the above, rerunning `diff-players` end to end:

```
matched (both sides, by FIDE id): 529403
ssp-only (no XML record for this FIDE id): 0
xml-only (no SSP record for this FIDE id): 0

family: 0 mismatches out of 529403 matched
given: 0 mismatches out of 529403 matched
suffix: 0 mismatches out of 529403 matched
federation: 0 mismatches out of 529403 matched
title: 0 mismatches out of 529403 matched
female (sex=F): 0 mismatches out of 529403 matched
birth_year: 0 mismatches out of 529403 matched
current_rating: 0 mismatches out of 529403 matched
aliases (as a set): 0 mismatches out of 529403 matched
peak (value, year, month): 0 mismatches out of 529403 matched
rating history (year->month grid): 0 mismatches out of 529403 matched
```

Exact parity on every field, for every player both sides agree exists,
across the full 529,403-player join — not a sample, not "close enough."
The SSP↔registry trust question is closed for the fields this diff
covers.

**One thing this diff does NOT cover, left unimplemented on purpose:**
the script merges multiple raw `@PLAYER` blocks that share one FIDE id
into a single `MergedPlayer` (union of titles/aliases, elo-conflict
resolution by record completeness). This parser treats each block as
independent and would silently let a later block overwrite an earlier
one in its `HashMap` if that ever happened. It doesn't matter for
*this* `.ssp` — checked directly: 0 FIDE ids appear in more than one raw
block in `ratings260801.ssp` — but would need implementing for
correctness against a `.ssp` where it does happen, e.g. one assembled by
merging multiple source captures.

**One provenance fact worth flagging, not yet acted on:** the registry's
own `source=` attribute says `ratings260703.ssp`; the file actually in
this repo is `ratings260801.ssp` — a different (later) snapshot. Every
match above held anyway, which says more about how little the FIDE-linked
subset of this data churns over that window than it resolves the
staleness — the registry has not actually been regenerated from the
`.ssp` this repo now carries.

**Performance:** the typed XML parse (building full structs — name,
aliases, complete rating history, not just counting) takes ~34s warm for
the same 4.6GB/27-shard registry that a plain count takes ~11.5s for.
Same rayon-per-shard note as `stats`'s applies, more so now that there's
real per-record work to parallelize.

## `ctml-clean ingest-crosstables` — crosstables.json → ctml:tournament, this session

`src/tournament.rs` + `src/xmlutil.rs` (new: `esc`/`normalize_space`/
`slug`/`xml_id`/`sha1_hex16`, generic enough to reuse later), wired up as
`ctml-clean ingest-crosstables [assets-dir] [out-dir]` (default
`out/tournaments`, gitignored — regenerable derived output, not a source
of truth). This is the first command that *writes* anything.

**Ported, not redesigned, from the actual reference implementation**:
`D:\dev\proj\ctml\readers\ctml_source_common.py::crosstable_to_ctml` —
found by locating the function `CHANGELOG.md` already cited by name,
after the project owner moved every script they could find into this
repo's new `scripts/` directory (the same move that resolved item (1)
above). Ported close to term-for-term, including exact XML element
order, because `xs:sequence` in `xsd/ctml-core.xsd` requires it — checked
directly, not assumed: read `TournamentType`/`ParticipantType`/
`PlayerRefType`/`PlaceRefType`/`PartialDateType` and every enum
(`EventFormatType`, `EventCadenceType`, `RatingSystemType`,
`RatingScopeType`, `PlayerTitleType`, `ResolutionMethodType`) the script
touches before porting a line, confirming this session's `xsd/` still
matches what the script targets (it does — no drift since the script was
written). `person_name_xml`'s family/given/suffix split is the same
algorithm as the player registry's (see `names.rs` above), so it reuses
that module rather than re-deriving it.

**Scope, stated up front rather than discovered later:** this converts
each of the 48,198 raw entries in `crosstables.json` independently. It
does **not** implement `scripts/curate_source_tournaments.py`'s
clustering step, which fuzzy-matches and merges multiple scrapers'
captures of the same real-world tournament (TWIC + OlimpBase +
chess-results + nwchess, etc.) before conversion — that step needs
several raw source trees this repo doesn't carry, only the one already-
scraped `crosstables.json`. Output is one `ctml:tournament` file per JSON
entry, not one per real-world tournament; cross-source dedup is a
separate, later task.

**Run against the real full file:**

```
parsing assets\crosstables.json ...
  48198 raw entries   896.41ms

wrote 38335 tournament files to out/tournaments
skipped 9858 (no usable start date), 5 (no event name / no named players)   77.53s
```

38335 + 9858 + 5 = 48198 — accounts for every entry. Both skip counts
were independently cross-checked with a raw Python pass over the same
file before trusting them: 9,858 entries with no `start`, 5 with no
non-blank player name — exact match.

**Verified against the schema, not just "it ran"**: every one of the
38,335 written files parsed and validated with `lxml.etree.XMLSchema`
against `xsd/ctml.xsd` — the full corpus, not a sample (17.6s total).
38,333/38,335 valid. The 2 failures are both genuine defects in the
*source* data, not this port, confirmed by checking that the reference
Python function has no guard against either case either:

- `twic-1616-ch-pol-u20-2025-suwalki.xml`: a scraped rating of `17067`
  (`RatingValueType` caps at 4000). `crosstable_to_ctml` emits
  `to_int(player.get("rating"))` straight through with no range check —
  Python would produce the same invalid element.
- `twic-438-19th-spring-fest-budapest-hun-hun-14-33-iii-2003.xml`: a
  scraped day of `33` (`DayType` caps at 31, structurally, independent of
  the conditional `@iso` attribute — checked in `ctml-dates.xsd`
  directly). Same story: `PartialDate`'s attrs are emitted unconditionally
  once `d` is `Some`, in both the Python and this Rust port.

Neither is a reason to distrust the other 38,333; both are pre-existing
gaps in the *reference* pipeline's date/rating sanity-checking that this
port faithfully reproduced rather than silently "fixed" (fixing them
here, differently from Python, would make the two implementations
diverge on behavior nobody decided to change).

**One data-quality observation, not a validity problem:** at least one
scraped "player" row is obviously junk (`out/tournaments/1857newyork-...`,
participant 17: name `"Confidence level:"` — a stray line the original
HTML-table scraper picked up as if it were a player). Valid CTML, real
garbage-in-garbage-out; the reference function has no name-plausibility
filtering either, so this isn't a regression, just worth knowing before
treating every synthesized participant as trustworthy.

**Performance:** 77.5s to write 38,335 small files (~671MB total) — the
conversion itself (JSON parse + all 48,198 `crosstable_to_ctml` calls) is
fast; the time is dominated by one `std::fs::write` syscall per file.
Worth revisiting (batched writes, or a different output shape entirely)
if this becomes a hot path rather than a one-shot run.

## Repo size — DECIDED, `.gitignore` updated

Attempting a first commit via GitHub Desktop surfaced GitHub's hard
100MB-per-file push limit: `ratings260801.ssp` (516MB), `crosstables.json`
(159MB), and 17 of 27 player-registry shards (largest 547MB) all exceed
it. Project owner's call: gitignore that reference data rather than fight
the limit (Git LFS, splitting files, etc. were not requested and weren't
pursued). `.gitignore` now excludes `assets/*.ssp`,
`assets/crosstables.json`, and all of `assets/registries/players/` — the
whole players directory, not just the shards currently over the limit,
since which shards cross that line shifts as the source `.ssp` grows and
partial tracking would be fragile. `assets/all.tsv`,
`assets/registries/events.xml`, and `assets/registries/places/` stay
tracked — none are large (69MB total for everything under `assets/` that
IS tracked). Nothing was ever committed before this change (repo was
still at 0 commits), so this cost no history.

## `ctml-clean fingerprint-selftest` — Zobrist fingerprint spec, this session

`src/chess.rs` (minimal board model: FEN parsing, UCI move application, no
legality checking — standard chess only, no Chess960, matching the spec's
own stated scope), `src/polyglot_array.rs` (the 781-entry Polyglot random
constant table), `src/fingerprint.rs` (the `trajectory`/`finalPosition`
accumulator), wired up as `ctml-clean fingerprint-selftest [spec-path]`.

**Nothing here was reconstructed from memory.** `spec/fingerprint.md`
requires byte-exact reproduction of `chess.polyglot.zobrist_hash()`, and
its own opening paragraph warns that a silent mismatch corrupts dedup
without raising an error — so every piece was read directly out of the
actual installed `python-chess 1.11.2` source before writing the Rust
equivalent, not inferred from general Zobrist-hashing knowledge:

- **The 781-entry random constant table** (`polyglot_array.rs`) was
  extracted *programmatically* from the installed
  `chess/polyglot.py::POLYGLOT_RANDOM_ARRAY` (a Python one-liner that
  imports the module and reformats the array as Rust source) rather than
  hand-transcribed — 781 sixteen-digit hex constants is exactly the kind
  of thing a manual retype gets subtly wrong once, invisibly.
- **The indexing scheme** (`hash_board`/`hash_castling`/
  `hash_ep_square`/`hash_turn` in `chess/polyglot.py::ZobristHasher`) was
  read directly: piece index `= (piece_type - 1) * 2 + color`, offsets
  768-771 for castling rights in white-kingside/white-queenside/
  black-kingside/black-queenside order, 772-779 for en-passant file (only
  hashed in if a pawn that could actually capture there exists — legality
  of the capture itself is irrelevant), 780 for side-to-move.
- **Castling-rights and en-passant bookkeeping through a move** (how
  `Board.push` actually updates them, not how a textbook describes it)
  was read from `chess/__init__.py`: a right is lost the instant *either
  endpoint* of *any* move touches one of the four corner squares (covers
  both "the rook moved" and "the rook got captured without ever moving"
  in one rule), separately from an unconditional clear on any king move;
  en passant capture is detected as a diagonal pawn move onto the
  previous ply's `ep_square` when the destination square was empty.

**Verified against the spec's own 6 test vectors, then extended.** All 6
original vectors pass, including the transposition-sensitivity pair (rows
4-5: identical `finalPosition`, different `trajectory` — the schema's
core guarantee). But those 6 never exercise castling, en passant, or
promotion — exactly the code paths a Zobrist port is most likely to get
wrong, per the spec's own opening paragraph. Rather than ship untested
coverage of the paths most likely to hide a bug, 4 more vectors were
generated (using the spec's own documented regeneration method, each
move sequence's legality checked with `chess.Board.is_legal()` before
trusting its hash) and appended to `spec/fingerprint.md` itself, in a new
section the spec already anticipates ("Regenerating or extending these
vectors"). All 10/10 pass:

```
found 10 test vectors in spec/fingerprint.md

PASS Empty (start position only)
PASS 1.e4
PASS 1.e4 e5
PASS 1.c4 e5 2.Nc3
PASS 1.Nc3 e5 2.c4 (transposition of the row above)
PASS Scholar's mate
PASS Kingside castling (white)
PASS Queenside castling (white)
PASS En passant capture
PASS Promotion (both sides, capturing)

all vectors match.
```

The self-test command parses the vector table straight out of the
Markdown file rather than hardcoding expected hex strings in Rust source
— same transcription-risk reasoning as the random-array extraction, and
it means `spec/fingerprint.md` stays the single source of truth: extend
the table, rerun `fingerprint-selftest`, nothing to keep in sync by hand.

**Scope note:** `compute()` takes UCI moves and an optional start FEN; it
does not parse PGN/SAN itself (that's the next item) and does not check
move legality (moves are trusted, per the spec's own "moves may be
consumed in any notation" framing — legality is PGN import's job, not
fingerprinting's).

## Legal move generator — this session, verified

`src/movegen.rs` on top of `src/chess.rs` (which gained `pub` accessors —
`turn`/`castling_rights`/`ep_square`/`piece_at`/`king_square` — and a
square-index `apply_move` factored out of `apply_uci`, plus `Clone`, so
movegen can apply a candidate to a scratch copy). Standard chess only, no
Chess960, same scope as `chess.rs`.

**Why this exists**: PGN import needs it, not fingerprinting. SAN
(`"Nf3"`, `"exd5"`, `"O-O"`) names a piece kind and a destination, and
resolving that to one `(from, to)` pair requires knowing every *legal*
move in the position — not just which pieces could geometrically reach
that square, since a pinned piece or a move that walks into check has to
be excluded. That's the actual reason a move generator had to come before
PGN parsing, not just a convenient place to start.

**Approach, stated plainly**: pseudo-legal generation per piece
(unconditional — doesn't check the mover's own king), then a legality
filter that clones the board, applies each candidate, and checks whether
the mover's own king is left in check. Correct and simple over fast — no
pin detection, no incremental check tracking. Matches this project's
established pattern of getting correctness verified first and profiling
once there's a real workload to profile against, not a guess.

**Verified against `python-chess`, not just against itself.** Generated
`spec/perft-vectors.tsv` by running `python-chess`'s own `board.legal_moves`
recursively (`perft`, the standard node-counting correctness check for a
move generator) against the six canonical "Perft Results" stress
positions from the chess-programming community — start position, Kiwipete
(castling + pins + promotions), position3 (en passant-heavy pawn
endgame), position4 and its color-mirrored twin (promotions + castling,
checked both colors independently rather than trusting symmetry), position5,
position6 — at depths chosen to keep total runtime reasonable (1-3 to
1-5 depending on branching factor). `ctml-clean movegen-selftest` reads
that TSV and reruns the same perft in Rust:

```
27 cases; all match.
```

27/27, including `startpos` depth 5 (4,865,609 nodes) and `kiwipete`
depth 4 (4,085,603 nodes) — both exact. `position4-mirrored` matching
independently of `position4` is worth calling out specifically: it's not
proof by symmetry (the two are different positions with different piece
sets, not literal mirror-move replays of each other), it's two separate
real cross-checks that happen to stress the same rules (castling,
promotion) from both colors' perspective.

**Performance, noted not chased**: Rust `perft(5)` on the start position
is 310ms; `python-chess`'s own recursive perft for the same took 7.94s
generating the vectors — about 25x, even with the deliberately
unoptimized clone-per-move approach above. Good enough that there's no
pressure to optimize before there's a real workload driving it.

## PGN import: SAN parsing + tag extraction — this session

`src/pgn.rs` (tokenizer), `src/san.rs` (SAN grammar + resolution against
`movegen::legal_moves`), `src/gameimport.rs` (ties tokenizer + SAN
resolver + `fingerprint::compute` together into a `ParsedGame`, and
renders `xsd/ctml-game.xsd`'s `GameType` — read directly before writing
the emitter, same as every other schema target this session). Wired up
as `ctml-clean pgn-import <file> [source-kind]` (diagnostic: parses,
imports, reports pass/fail, prints one example `<ctml:game>`) and
`ctml-clean pgn-selftest`.

**Why the move generator had to come first**: SAN (`"Nf3"`, `"exd5"`,
`"O-O"`) names a piece kind and destination, sometimes with a
disambiguating file/rank — resolving it to one `(from, to)` requires the
position's actual legal-move list (a pinned piece or a move into check
has to be excluded), which is exactly what `movegen.rs` provides.
`san::resolve` filters `legal_moves(board)` down by piece kind,
destination, promotion, and disambiguator, erroring distinctly on zero
matches vs. more than one (both are real parse failures, not the same
failure for debugging).

**Tokenizer scope, stated plainly**: extracts mainline SAN tokens only —
move numbers, comments (`{...}` and `;...`), NAGs (`$n`), and variations
(`(...)`, which can nest) are consumed and discarded, matching
`scripts/pgn_to_ctml.py`'s own scope (it only ever stores
`game.mainline_moves()`; CTML's `ctml:moves` has no home for comments or
side lines regardless).

**One real bug, found by testing against real PGN text, not invented
edge cases:** the first self-test run (5 hand-picked games + 60 random
legal self-play games, generated via `python-chess`, expected UCI move
lists computed by the same) came back 22/65 passing — every failure was
a SAN token ending in `+` or `#`. The tokenizer stripped informal `!`/`?`
annotation glyphs but the actual check/mate markers were never in that
strip set at all — an omission, not a subtle bug, and exactly the kind
of thing that's invisible until tested against text that actually
contains a check. One-line fix (`trim_end_matches(['!', '?', '+', '#'])`
instead of `['!', '?']`); reran clean: **65/65**.

**Then verified against real-world data, not just generated test cases —
the entire file, not a sample.** `scripts/pgn_to_ctml.py`'s own docstring
names its production input as `mega-database-2025-filtered.pgn`; it
turned out to still be on disk
(`D:\..Bookstacks\mega-database-2025-filtered.pgn`, 1.58GB, real games
spanning 1821 to present — counted precisely with `grep -cE
'^\[Event "'`, not the sloppier `'^\[Event'` which also matches
`EventDate`/`EventCountry` and overcounts by ~4x). A 200MB/252,136-game
slice ran first (252,136/252,136, zero failures, 101s), then the full
file:

```
parsed 1881897 games from mega-database-2025-filtered.pgn     49.58s
imported 1881897/1881897 games (0 failed), 157648988 total plies    810.72s
```

**1,881,897/1,881,897 — every real game in the actual production
database, zero failures**, ~157.6M plies total, ~2,320 games/sec (move
resolution phase) with no throughput degradation from the 200MB sample to
the full 1.58GB file. Two centuries of real, messy PGN — mixed
conventions, unknown dates, every check/mate marker, every castling and
promotion and en passant a real game corpus contains — not a
hand-picked or synthetic set.

**Explicit scope boundary, not yet done:** this converts one game at a
time. It does **not** implement `pgn_to_ctml.py`'s tournament-grouping
(games clustered by normalized Event+Site, split on a 21-day gap),
registry resolution (player/event/place index lookups against the real
registries), the rating-floor admission rule, or dedup-safe corpus
writing (`corpus_writer.py`'s merge-by-fingerprint logic) — all of that
is a distinct, separate layer on top of what's built here, not
discovered scope creep to chase down now.

## Next real unit of work

Per the project owner, in order: (1) Zobrist fingerprint spec — done.
(1b) Legal move generator — done. (2) PGN import — done for single-game
SAN resolution + tag extraction (see above); tournament
grouping/registry-resolution/corpus-writing is real remaining scope
within "PGN import" broadly, not yet started. (3) Cross-source
tournament dedup/clustering (the `curate_source_tournaments.py` gap noted
earlier) — overlaps significantly with item 2's remaining scope, since
both need the same event-clustering logic; likely worth doing together
rather than as strictly sequential items. Say how you want to sequence
that, or something else.
