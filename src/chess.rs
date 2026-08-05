//! Minimal chess board model: just enough to *apply* an already-legal UCI
//! move sequence (no legality checking, no move generation) and compute
//! `chess.polyglot.zobrist_hash()`-compatible Zobrist hashes byte-for-byte.
//! Standard chess only — no Chess960 — matching `spec/fingerprint.md`'s
//! stated test scope.
//!
//! Every rule here (castling-rights bookkeeping, en-passant bookkeeping,
//! the Zobrist indexing scheme itself) was read directly out of the
//! installed `python-chess 1.11.2` source
//! (`chess/__init__.py::Board.push`, `chess/polyglot.py::ZobristHasher`)
//! rather than reconstructed from memory — see `polyglot_array.rs` for the
//! same discipline applied to the random constant table.

use crate::polyglot_array::POLYGLOT_RANDOM_ARRAY;

pub const BLACK: u8 = 0;
pub const WHITE: u8 = 1;

pub const PAWN: u8 = 1;
pub const KNIGHT: u8 = 2;
pub const BISHOP: u8 = 3;
pub const ROOK: u8 = 4;
pub const QUEEN: u8 = 5;
pub const KING: u8 = 6;

#[derive(Clone, Copy)]
struct Piece {
    color: u8,
    kind: u8,
}

#[derive(Clone)]
pub struct Board {
    squares: [Option<Piece>; 64],
    turn: u8,
    /// `[white_kingside, white_queenside, black_kingside, black_queenside]`
    /// — matches `POLYGLOT_RANDOM_ARRAY[768..=771]`'s order exactly.
    castling: [bool; 4],
    ep_square: Option<u8>,
}

fn file(sq: u8) -> u8 {
    sq % 8
}

fn rank(sq: u8) -> u8 {
    sq / 8
}

fn piece_char(kind: u8) -> char {
    match kind {
        PAWN => 'p',
        KNIGHT => 'n',
        BISHOP => 'b',
        ROOK => 'r',
        QUEEN => 'q',
        KING => 'k',
        _ => unreachable!("invalid piece kind {kind}"),
    }
}

pub(crate) fn char_piece(c: char) -> Option<(u8, u8)> {
    let kind = match c.to_ascii_lowercase() {
        'p' => PAWN,
        'n' => KNIGHT,
        'b' => BISHOP,
        'r' => ROOK,
        'q' => QUEEN,
        'k' => KING,
        _ => return None,
    };
    let color = if c.is_ascii_uppercase() { WHITE } else { BLACK };
    Some((color, kind))
}

/// `"e4"` -> square index (a1=0 .. h8=63, matching python-chess's own
/// `chess.SQUARE_NAMES` / `square = rank * 8 + file` convention).
pub(crate) fn square_from_algebraic(s: &str) -> Option<u8> {
    let bytes = s.as_bytes();
    if bytes.len() != 2 {
        return None;
    }
    let f = bytes[0].checked_sub(b'a')?;
    let r = bytes[1].checked_sub(b'1')?;
    if f > 7 || r > 7 {
        return None;
    }
    Some(r * 8 + f)
}

/// Inverse of [`square_from_algebraic`]: square index -> `"e4"`.
pub(crate) fn square_to_algebraic(sq: u8) -> String {
    format!("{}{}", (b'a' + file(sq)) as char, (b'1' + rank(sq)) as char)
}

pub const STARTING_FEN: &str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

impl Board {
    pub fn starting_position() -> Board {
        Board::from_fen(STARTING_FEN).expect("STARTING_FEN is well-formed")
    }

    /// Parses the four fields Zobrist hashing needs (piece placement,
    /// turn, castling availability, en-passant target) out of a FEN
    /// string; ignores halfmove clock / fullmove number if present (they
    /// don't participate in `zobrist_hash`).
    pub fn from_fen(fen: &str) -> Option<Board> {
        let mut fields = fen.split_whitespace();
        let placement = fields.next()?;
        let turn_field = fields.next().unwrap_or("w");
        let castling_field = fields.next().unwrap_or("-");
        let ep_field = fields.next().unwrap_or("-");

        let mut squares: [Option<Piece>; 64] = [None; 64];
        let ranks: Vec<&str> = placement.split('/').collect();
        if ranks.len() != 8 {
            return None;
        }
        for (rank_from_top, rank_str) in ranks.iter().enumerate() {
            let rank_idx = 7 - rank_from_top as u8; // FEN lists rank8 first
            let mut file_idx = 0u8;
            for ch in rank_str.chars() {
                if let Some(n) = ch.to_digit(10) {
                    file_idx += n as u8;
                } else {
                    let (color, kind) = char_piece(ch)?;
                    if file_idx > 7 {
                        return None;
                    }
                    squares[(rank_idx * 8 + file_idx) as usize] = Some(Piece { color, kind });
                    file_idx += 1;
                }
            }
        }

        let turn = if turn_field == "b" { BLACK } else { WHITE };

        let mut castling = [false; 4];
        for ch in castling_field.chars() {
            match ch {
                'K' => castling[0] = true,
                'Q' => castling[1] = true,
                'k' => castling[2] = true,
                'q' => castling[3] = true,
                _ => {}
            }
        }

        let ep_square = if ep_field == "-" { None } else { square_from_algebraic(ep_field) };

        Some(Board { squares, turn, castling, ep_square })
    }

    /// Applies one already-legal UCI move (`"e2e4"`, `"e7e8q"`,
    /// `"e1g1"` for castling). No legality checking — the caller (PGN
    /// import) is expected to only ever hand this real, legal moves.
    pub fn apply_uci(&mut self, uci: &str) -> Option<()> {
        let bytes = uci.as_bytes();
        if bytes.len() < 4 {
            return None;
        }
        let from = square_from_algebraic(&uci[0..2])?;
        let to = square_from_algebraic(&uci[2..4])?;
        let promotion = if bytes.len() >= 5 { char_piece(uci.as_bytes()[4] as char).map(|(_, k)| k) } else { None };
        self.apply_move(from, to, promotion)
    }

    /// Same as [`Self::apply_uci`], taking square indices and a decoded
    /// promotion piece directly — [`crate::movegen`] generates candidate
    /// moves this way and needs to apply/undo them without a UCI-string
    /// round-trip. No legality checking here either: `movegen` is what
    /// filters pseudo-legal moves down to legal ones, by applying each to
    /// a clone and checking whether it leaves the mover's own king in
    /// check.
    pub fn apply_move(&mut self, from: u8, to: u8, promotion: Option<u8>) -> Option<()> {
        let moving = self.squares[from as usize]?;
        let mut captured = self.squares[to as usize];
        let prev_ep = self.ep_square;
        self.ep_square = None;

        let diff = to as i32 - from as i32;
        let is_pawn = moving.kind == PAWN;

        // En-passant capture: a pawn moving diagonally onto last move's ep
        // square, which is empty (an ordinary diagonal move is always a
        // capture, so an *empty* destination for a diagonal pawn move can
        // only be this).
        if is_pawn && Some(to) == prev_ep && captured.is_none() && (diff.abs() == 7 || diff.abs() == 9) {
            let down: i32 = if moving.color == WHITE { -8 } else { 8 };
            let capture_sq = (prev_ep.unwrap() as i32 + down) as u8;
            captured = self.squares[capture_sq as usize];
            self.squares[capture_sq as usize] = None;
        }

        // New en-passant square from a two-square pawn push.
        if is_pawn {
            if diff == 16 && rank(from) == 1 {
                self.ep_square = Some(from + 8);
            } else if diff == -16 && rank(from) == 6 {
                self.ep_square = Some(from - 8);
            }
        }

        // Castling rights: lost the instant either endpoint of *any* move
        // touches one of the four home-corner squares (covers the rook
        // moving away, and an enemy piece capturing a rook that never
        // moved), plus unconditionally on any king move regardless of
        // destination. Mirrors `Board.push`'s
        // `castling_rights &= ~to_bb & ~from_bb` exactly.
        for touched in [from, to] {
            match touched {
                0 => self.castling[1] = false,  // a1: white queenside
                7 => self.castling[0] = false,  // h1: white kingside
                56 => self.castling[3] = false, // a8: black queenside
                63 => self.castling[2] = false, // h8: black kingside
                _ => {}
            }
        }
        if moving.kind == KING {
            if moving.color == WHITE {
                self.castling[0] = false;
                self.castling[1] = false;
            } else {
                self.castling[2] = false;
                self.castling[3] = false;
            }
        }

        let is_castle = moving.kind == KING && diff.abs() == 2;

        self.squares[from as usize] = None;
        let final_kind = promotion.unwrap_or(moving.kind);
        self.squares[to as usize] = Some(Piece { color: moving.color, kind: final_kind });
        let _ = captured; // captured piece is simply overwritten/removed above

        if is_castle {
            let rank_base = if moving.color == WHITE { 0u8 } else { 56u8 };
            if diff == 2 {
                // kingside: rook h-file -> f-file
                self.squares[(rank_base + 7) as usize] = None;
                self.squares[(rank_base + 5) as usize] = Some(Piece { color: moving.color, kind: ROOK });
            } else {
                // queenside: rook a-file -> d-file
                self.squares[rank_base as usize] = None;
                self.squares[(rank_base + 3) as usize] = Some(Piece { color: moving.color, kind: ROOK });
            }
        }

        self.turn = if self.turn == WHITE { BLACK } else { WHITE };
        Some(())
    }

    /// `chess.polyglot.zobrist_hash(board)`, byte-for-byte — see
    /// `ZobristHasher` in the installed `python-chess` source for the
    /// scheme this mirrors.
    pub fn zobrist_hash(&self) -> u64 {
        let mut h: u64 = 0;

        for sq in 0u8..64 {
            if let Some(p) = self.squares[sq as usize] {
                let piece_index = ((p.kind - 1) as usize) * 2 + p.color as usize;
                h ^= POLYGLOT_RANDOM_ARRAY[64 * piece_index + sq as usize];
            }
        }

        if self.castling[0] {
            h ^= POLYGLOT_RANDOM_ARRAY[768];
        }
        if self.castling[1] {
            h ^= POLYGLOT_RANDOM_ARRAY[769];
        }
        if self.castling[2] {
            h ^= POLYGLOT_RANDOM_ARRAY[770];
        }
        if self.castling[3] {
            h ^= POLYGLOT_RANDOM_ARRAY[771];
        }

        if let Some(ep) = self.ep_square {
            let capture_rank: i32 = if self.turn == WHITE { rank(ep) as i32 - 1 } else { rank(ep) as i32 + 1 };
            let mut has_capturer = false;
            if (0..8).contains(&capture_rank) {
                for df in [-1i32, 1i32] {
                    let cf = file(ep) as i32 + df;
                    if (0..8).contains(&cf) {
                        let csq = (capture_rank * 8 + cf) as usize;
                        if let Some(p) = self.squares[csq] {
                            if p.color == self.turn && p.kind == PAWN {
                                has_capturer = true;
                            }
                        }
                    }
                }
            }
            if has_capturer {
                h ^= POLYGLOT_RANDOM_ARRAY[772 + file(ep) as usize];
            }
        }

        if self.turn == WHITE {
            h ^= POLYGLOT_RANDOM_ARRAY[780];
        }

        h
    }

    // -------------------------------------------------------------
    // Accessors for crate::movegen. Kept as accessors (rather than
    // making the fields themselves `pub(crate)`) so this file stays the
    // one place that knows the board's internal representation.
    // -------------------------------------------------------------

    pub fn turn(&self) -> u8 {
        self.turn
    }

    pub fn castling_rights(&self) -> [bool; 4] {
        self.castling
    }

    pub fn ep_square(&self) -> Option<u8> {
        self.ep_square
    }

    /// `(color, piece_type)` at `sq`, or `None` if empty.
    pub fn piece_at(&self, sq: u8) -> Option<(u8, u8)> {
        self.squares[sq as usize].map(|p| (p.color, p.kind))
    }

    pub fn king_square(&self, color: u8) -> Option<u8> {
        (0u8..64).find(|&sq| self.squares[sq as usize].is_some_and(|p| p.color == color && p.kind == KING))
    }

    /// For diagnostics only — not used by the fingerprint path.
    #[allow(dead_code)]
    pub fn board_fen(&self) -> String {
        let mut out = String::new();
        for rank_idx in (0..8u8).rev() {
            let mut empty = 0u32;
            for file_idx in 0..8u8 {
                match self.squares[(rank_idx * 8 + file_idx) as usize] {
                    None => empty += 1,
                    Some(p) => {
                        if empty > 0 {
                            out.push_str(&empty.to_string());
                            empty = 0;
                        }
                        let c = piece_char(p.kind);
                        out.push(if p.color == WHITE { c.to_ascii_uppercase() } else { c });
                    }
                }
            }
            if empty > 0 {
                out.push_str(&empty.to_string());
            }
            if rank_idx > 0 {
                out.push('/');
            }
        }
        out
    }
}
