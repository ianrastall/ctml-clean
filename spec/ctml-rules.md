# CTML application-level rules (what XSD 1.0 can't check)

The schema set in `xsd/` is XSD 1.0 by deliberate choice (see `ctml-core.xsd`'s
own annotation and `docs/HANDOFF.md` in the pipeline project for the reasoning:
no tooling caveats anywhere, in any language, versus real gaps in XSD 1.1
assertion support in both .NET and Python). XSD 1.0 has no mechanism for true
cross-sibling or cross-attribute value comparisons — `xs:choice` only
constrains *which elements are present*, not *what values they hold relative
to each other*. Every such rule that would have been an XSD 1.1 `xs:assert`
is deliberately pushed to application code instead, and each push is already
noted inline at its point of relevance in the `.xsd` files. This document
exists to collect them in one place, so a new implementation (a Rust reader,
a validator, a second pipeline) has a checklist instead of having to
rediscover each rule by reading every module's comments individually.

Honesty markers, matching the convention used elsewhere in this project:
- **IMPLEMENTED** — enforced today, in the Python import pipeline
  (`D:\dev\proj\ctml\readers`, `scripts`). Cite the actual check if you're
  verifying this list against code.
- **NOT YET IMPLEMENTED** — a real gap, not yet enforced anywhere. Valid
  XML matching the XSD can currently violate these; nothing currently
  stops it.
- **RECOMMENDED** — a check this document proposes adding, not required by
  any existing decision. Flagged as an addition, not a correction.

## Rules already called out in the schema's own comments

### 1. Date-range ordering: `start <= end`

Where: `ctml:DateRangeType` (`ctml-dates.xsd`), used by
`TournamentHeaderType/dates` and `EventOccurrenceType/dates`.

Rule: when both `start` and `end` are day-precision (`<day>` with `@iso`),
`start/@iso` must be `<=` `end/@iso`. At year or month precision there is no
`@iso` to compare, so the check only applies at day precision; do not attempt
to fabricate a day just to run this comparison at coarser precisions.

Status: **NOT YET IMPLEMENTED**. `docs/HANDOFF.md` documents this as an
open, acknowledged gap ("a true cross-sibling comparison, which XSD 1.0
genuinely cannot express... moves to the Python import pipeline") but no
script in `readers/` or `scripts/` currently runs it. It is cheap to add
(one comparison, only reachable at day precision) and should be added before
this is forgotten as "already handled."

### 2. A game's two sides are different participants: `@white != @black`

Where: `ctml:GameType` (`ctml-game.xsd`), inside `ctml:GamesType`
(`ctml-core.xsd`).

Rule: `game/@white` and `game/@black` must not be the same participant
`@id`. XSD's `xs:IDREF` mechanism guarantees each resolves to *some*
existing ID in the document, but does not compare the two values against
each other.

Status: **NOT YET IMPLEMENTED** for the same reason as rule 1 — documented
as a known gap, not wired into any script yet.

### 3. No duplicate ply numbers within one move list

Where: `ctml:MovesType`'s `UniquePlyPerGame` identity constraint
(`ctml-game.xsd`).

Rule (partially native): `xs:unique` already rejects two `<move>` elements
sharing a `@ply` value — this part is **IMPLEMENTED structurally**, enforced
by any conforming XSD 1.0 processor, no application code required.

What XSD does *not* catch, and what stays application-level:
- **Gaps** — a move list containing ply 1, 2, 4 (skipping 3) is
  schema-valid.
- **Non-sequential order** — plies out of ascending order (3, 1, 2) are
  schema-valid; `xs:unique` doesn't imply or check ordering.
- **Ply-1 start** — nothing requires the lowest `@ply` in a list to be 1.

Status: gap portions are **NOT YET IMPLEMENTED**. Recommended check: after
sorting `<move>` elements by document order (or by `@ply`), assert the
sorted `@ply` sequence is exactly `1, 2, ..., plyCount` with no gaps, and
that document order matches ascending `@ply` order (a reordered-but-complete
move list is a stronger and stranger anomaly than a mere gap, worth
distinguishing in an error message).

### 4. No duplicate rating year within one rating track's history

Where: `ctml:MonthlyRatingsType` (`ctml-entities.xsd`). The existing
`UniqueMonthNumPerYear` constraint only guards uniqueness of `month/@num`
*within* a single `<year>` element — it says nothing about two sibling
`<year>` elements both claiming `@value="2005"`.

Status: **NOT YET IMPLEMENTED**, and not previously documented as a known
gap — found while writing this document, not carried over from an existing
note. **RECOMMENDED**: add an `xs:unique` constraint
(`UniqueYearInMonthlyRatings`, selector `ctml:year`, field `@value`) the same
way `UniqueMonthNumPerYear` was added — this one actually *can* be expressed
natively in XSD 1.0, unlike rules 1–3, so it belongs in the schema itself
rather than in application code. Not yet added here because it changes
validation behavior for existing registry data and should be checked against
the real 617,358-player registry before being turned on, per this project's
own practice of testing schema changes against real data before considering
them done.

## Rules identified while writing this document (not previously called out)

These are genuine gaps found by reading the assembled schema end to end
looking specifically for cross-field consistency, not carried over from any
existing comment. None of these are structural defects — the schema
correctly leaves them to application code, consistent with the rest of the
1.0/app-code split — they just weren't written down anywhere as a checklist
item before now.

### 5. IDREF attributes must resolve to the right *kind* of element

Where: `game/@white`, `game/@black`, `nonGame/@participant`,
`nonGame/@opponent` (all `xs:IDREF`, `ctml-core.xsd`/`ctml-game.xsd`).

Rule: `xs:IDREF` only guarantees the referenced value matches *some*
`xs:ID` attribute somewhere in the same document — it does not check that
the referencing attribute points at a `<participant>` element specifically.
A malformed document where `game/@white` accidentally holds the
`tournament/@id` value (also an `xs:ID`) or another `game/@id` would pass
schema validation.

Status: **NOT YET IMPLEMENTED**. **RECOMMENDED**: after schema validation,
confirm every `@white`/`@black`/`@participant`/`@opponent` value resolves to
an element that is specifically a `ctml:participant` child of the same
tournament's `ctml:participants`.

### 6. `termination` and `result` should be mutually consistent

Where: `ctml:GameType` (`ctml-game.xsd`) — `@result`
(`ctml:GameResultType`) and `termination` (`ctml:TerminationType`) are
independent, unconstrained-against-each-other fields.

Rule (semantic, not structural): certain combinations are contradictory on
their face — `termination="stalemate"` implies `result="1/2-1/2"`;
`termination="checkmate"` implies a decisive result (`1-0` or `0-1`), never
a draw or `0-0`. A document with `termination="checkmate"
result="1/2-1/2"` is nonsensical but schema-valid.

Status: **NOT YET IMPLEMENTED** anywhere. **RECOMMENDED** as a data-quality
lint (flag, don't necessarily reject outright — source data is occasionally
just wrong, and quarantining rather than hard-rejecting matches this
project's general stance of not silently discarding data). Not every
`termination` value constrains `result` this tightly (e.g. `normal`,
`agreement`, `adjudication`, `forfeit`, `abandoned`, `unknown`, `timeout`
are compatible with more than one result), so this is a partial lookup
table, not a total function — implement it as such rather than trying to
force every combination into a strict mapping.

### 7. `moves/@plyCount`, if present, should equal the actual move count

Where: `ctml:MovesType` (`ctml-game.xsd`) — `@plyCount` is optional and
purely declarative; nothing checks it against the actual number of
`<move>` children.

Status: **NOT YET IMPLEMENTED**. **RECOMMENDED**, cheap: `plyCount ==
count(move)` whenever `@plyCount` is present. A mismatch most likely means
a partial/truncated import rather than deliberate data, so this is a good
low-cost early-warning check.

### 8. `fingerprint` scope discipline is a process rule, not a document-validity rule

Where: `ctml:FingerprintSetType` (`ctml-analysis.xsd`); see `spec/fingerprint.md`
for the full scheme.

This one is different in kind from rules 1–7: nothing about a single,
isolated CTML document is invalid if `scope="finalPosition"` and
`scope="trajectory"` happen to hold the same value (that can legitimately
happen for a very short game where the whole trajectory hash and the final
Zobrist hash both fit their respective definitions independently). The rule
is entirely about *how a consumer uses these values*: **trajectory only,
never finalPosition, for dedup decisions.** No schema or single-document
check can express "don't use field X for purpose Y" — this has to live in
the dedup code itself, and already does (`readers/corpus_writer.py`'s
game-identity logic never reads `scope="finalPosition"`). Listed here for
completeness of the checklist, not because it's a gap.

## Deliberately not listed here

- Rules already converted to native XSD 1.0 constructs (the `xs:choice`
  precision pattern in `PartialDateType`, `NonBlankStringType`'s pattern
  restriction on `PersonNameType/@display`, `UniqueMonthNumPerYear`,
  `UniquePlyPerGame`'s duplicate-detection half) are enforced by any
  conforming XSD 1.0 processor and need no application code. They're
  covered by validating against `ctml.xsd`, not by this document.
- Corpus-level policy (the 2000-Elo admission floor, "admit tournaments
  whole, never partial," source precedence) lives in
  `docs/corpus-policy.md` in the pipeline project. Those are curation
  policy decisions about which documents get published, not rules about
  whether a single document is well-formed CTML — a different layer,
  deliberately kept in a different document.
