# CTML game fingerprint scheme: `zobrist-polyglot-1`

Status: implemented and in production use. This document exists because
`ctml-analysis.xsd`'s own annotation says the schema pins `scheme` as an
explicit token specifically so an interop hazard doesn't happen silently —
this file is that pinned specification, not a proposal. Any second
implementation (a Rust reader, a from-scratch reimplementation, a future
`zobrist-polyglot-2`) must reproduce every byte-level choice below exactly,
or it will silently compute different fingerprints for the same game and
corrupt deduplication without raising an error.

The reference implementation is `readers/fingerprint.py` in the Python
pipeline (`D:\dev\proj\ctml`), built on `python-chess`'s
`chess.polyglot.zobrist_hash()` — the standard Polyglot Zobrist scheme —
rather than a hand-rolled hash. That choice is deliberate: a from-scratch
Zobrist implementation's most common bugs are in castling-rights and
en-passant state, and reusing a widely-used, already-correct implementation
avoids creating a new place for that exact bug to reappear.

## Why two scopes, and why they must never be conflated

`ctml:fingerprint` has two `@scope` values, and confusing them is the actual
bug risk this design guards against, not a hypothetical one:

- **`trajectory`** — hash of the *entire move sequence*. This is the real
  identity/dedup key.
- **`finalPosition`** — hash of the *endpoint position only*. A separate,
  optional feature for future position-search. **Must never be used for
  dedup.** Many unrelated games — especially short draws and common
  endgames — converge on the same final position; two games reaching the
  same final position are not the same game.

## Byte-exact definition

Both scopes start from the same primitive:

```text
zobrist_bytes(position) = big-endian 8-byte encoding of
                           chess.polyglot.zobrist_hash(position)
```

`chess.polyglot.zobrist_hash()` returns a 64-bit unsigned integer; pack it
as 8 bytes, most-significant byte first (Python: `struct.pack(">Q", value)`).

### `scope="finalPosition"`

```text
value = hex(zobrist_bytes(final_position))     — 16 hex characters
```

The raw Zobrist value, hex-encoded, with **no second hash layer**. This is
deliberate: a future position-search index wants the raw Zobrist value
directly, since fast incremental/positional comparison is the entire point
of using this scheme for that purpose.

### `scope="trajectory"`

```text
value = hex(SHA256(zobrist_bytes(p0) || zobrist_bytes(p1) || ... || zobrist_bytes(pN)))
```

— 64 hex characters, where `p0` is the **starting position** (ply 0, before
any move) and `pN` is the position after the final move. Concatenate the
raw 8-byte Zobrist encodings of every position in order, including `p0`,
then take one SHA-256 over the whole concatenation.

Including `p0` is not an edge-case nicety: two games with an identical move
sequence but different starting FENs (e.g. a Chess960 or odds-game start)
must get different trajectory fingerprints, and omitting `p0` would silently
collapse that distinction whenever the actual moves happen to coincide (an
empty-move-list game is the sharpest case — see the test vectors below).

`@scheme="zobrist-polyglot-1"` is written on both fingerprint elements. A
future encoding change gets a new token (`zobrist-polyglot-2`, or an
entirely different scheme) so that old and new fingerprints can coexist
during a transition instead of forcing a one-shot reinterpretation of
already-stored hex values.

## Reference algorithm (pseudocode)

```text
function compute_fingerprints(moves, start_position):
    hasher = SHA256()
    position = start_position
    zb = zobrist_bytes(position)
    hasher.update(zb)                    # ply 0
    for move in moves:
        position = apply(position, move)
        zb = zobrist_bytes(position)
        hasher.update(zb)
    trajectory = hex(hasher.digest())    # 64 hex chars
    final_position = hex(zb)             # 16 hex chars, last zb computed
    return trajectory, final_position
```

`moves` may be consumed in any notation (UCI/SAN/LAN) — the fingerprint
depends only on the resulting position sequence, not on how CTML happens to
store the move (`moves/@notation`).

## Test vectors

Computed against `python-chess 1.11.2` (`chess.polyglot.zobrist_hash`),
2026-08-05. All positions are standard chess (no Chess960), moves given in
UCI. Any conforming implementation MUST reproduce these exact hex strings.

| Case | Moves (UCI) | `trajectory` | `finalPosition` |
|---|---|---|---|
| Empty (start position only) | *(none)* | `89dffe8f5406d6237e8428fc8a89912f97175c7b72d3282aa4216868247cfbae` | `463b96181691fc9c` |
| 1.e4 | `e2e4` | `f4caae704c166a7e30380f4ade3a580e72218d6429885435fed80e08fdede0d1` | `823c9b50fd114196` |
| 1.e4 e5 | `e2e4 e7e5` | `64ad2d50921adf0d62d0dfdf76b5625379628b34e60a86e40e5605fac6742fab` | `0844931a6ef4b9a0` |
| 1.c4 e5 2.Nc3 | `c2c4 e7e5 b1c3` | `bc9bc142c93f4c34ce5097cd09315da7e20173ef3aa052e2267b4908be818d3b` | `bbf719d404992d74` |
| 1.Nc3 e5 2.c4 (transposition of the row above) | `b1c3 e7e5 c2c4` | `e386f8ee8bbf00145718d9b07d3276f3445e91db6771c6f8712460a2280695c4` | `bbf719d404992d74` |
| Scholar's mate | `e2e4 e7e5 f1c4 b8c6 d1h5 g8f6 h5f7` | `1b94d3d89d69e839e0338b20c40e71b461ac347d501ebe929408ab2a90037129` | `c3116e611017a62f` |

Three properties these vectors are chosen to prove, not just illustrate:

1. **Transposition sensitivity vs. position convergence.** Rows 4 and 5
   reach the *same* final position by a *different* move order:
   `finalPosition` is identical (`bbf719d4...`) but `trajectory` differs
   (`bc9bc142...` vs `e386f8ee...`). This is the schema's core guarantee
   working as designed — if any implementation produces matching
   trajectories for these two rows, it has a transposition bug.
2. **Starting-position sensitivity.** The "Empty" row's `trajectory` is
   computed from `p0` alone (no moves) and is a 64-character SHA-256 of a
   single 8-byte block, not an empty-input hash — proving ply 0 is actually
   included, not skipped when the move list is empty.
3. **Byte lengths are fixed**: `finalPosition` is always 16 hex characters
   (8 bytes); `trajectory` is always 64 hex characters (32 bytes),
   regardless of game length.

### Regenerating or extending these vectors

```python
import chess, chess.polyglot, hashlib, struct

def zobrist_bytes(board):
    return struct.pack(">Q", chess.polyglot.zobrist_hash(board))

def fingerprints(uci_moves, start_fen=None):
    board = chess.Board(start_fen) if start_fen else chess.Board()
    h = hashlib.sha256()
    zb = zobrist_bytes(board)
    h.update(zb)
    for uci in uci_moves:
        board.push(chess.Move.from_uci(uci))
        zb = zobrist_bytes(board)
        h.update(zb)
    return h.hexdigest(), zb.hex()
```

This is a transcription of `readers/fingerprint.py`'s `FingerprintAccumulator`
into a standalone form for anyone verifying a new implementation without
importing the pipeline. The pipeline itself feeds `FingerprintAccumulator`
the board after every ply during a move-walk it is already doing (to extract
UCI moves for storage), so fingerprinting adds no second replay pass over
the game — a new implementation is free to do the same, or to replay
separately; the byte-exact rules above are what must match, not the code
shape.

### Additional vectors: castling, en passant, promotion

The six vectors above (added when this spec was written) never exercise
castling-rights changes, an en-passant capture, or a promotion — real gaps
in coverage for anyone porting this to a second implementation, since
those are exactly where a Zobrist port is most likely to have a bug (per
this doc's own opening paragraph). Added 2026-08-05, using the
regeneration method above, when the first non-Python implementation (Rust,
`ctml-clean`) was built — each move sequence's legality was checked with
`chess.Board.is_legal()` before trusting its hash, not just assumed.

| Case | Moves (UCI) | `trajectory` | `finalPosition` |
|---|---|---|---|
| Kingside castling (white) | `e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 e1g1` | `968f46864648853b6831143e240bdf3718e2d55b0ae5b287941a3e22125eae5a` | `c8162c4989019aab` |
| Queenside castling (white) | `d2d4 d7d5 b1c3 b8c6 c1f4 c8f5 d1d2 d8d7 e1c1` | `d7c881a8e5d62ba68e04b7d0c0d702b0895ec948b3872e6b1a2cf33daecc5cdd` | `03f59b930d475ebb` |
| En passant capture | `e2e4 a7a6 e4e5 d7d5 e5d6` | `9d14f71fcfdd1044f7bd07354edf75d7f05bb04d59ebcccefbe915e86d0ff209` | `49d76e2d212d595c` |
| Promotion (both sides, capturing) | `a2a4 h7h5 a4a5 h5h4 a5a6 h4h3 a6b7 h3g2 b7a8q g2h1q` | `f2080091bc06fa8692cdd55a15d5035f286e6f70d23fbf9643638962b40701b9` | `e23dff4ec4e43cdd` |

These four don't prove anything the original six don't about
transposition/starting-position sensitivity — that's what rows 4-5 above
already cover — they exist purely to catch a castling-rights, en-passant,
or promotion bug that the original six would silently miss.

## Non-goals

- No claim of cryptographic hardness is made or needed for `trajectory`;
  SHA-256 is used for its collision resistance at this data scale and
  convenient fixed length, not for any security property.
- `finalPosition` is intentionally a raw, unsalted, single-layer hash —
  again, not a security boundary, just a position-index key.
