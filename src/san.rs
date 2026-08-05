//! SAN (Standard Algebraic Notation) parsing and resolution. A SAN token
//! (`"Nf3"`, `"exd5"`, `"O-O"`, `"e8=Q+"`, `"Nbd7"`) names a piece kind,
//! a destination, and — only when needed to disambiguate between two or
//! more of the mover's own pieces that could reach the same square — a
//! source file and/or rank. Resolving it to one actual `(from, to)` move
//! requires the position's full legal-move list from
//! [`crate::movegen`]; this module only knows SAN grammar, not chess
//! rules.

use crate::chess::{char_piece, square_from_algebraic, square_to_algebraic, Board, KING, PAWN};
use crate::movegen::{self, Move};

#[derive(Debug)]
enum SanMove {
    CastleKingside,
    CastleQueenside,
    Normal { piece: u8, dest: u8, from_file: Option<u8>, from_rank: Option<u8>, promotion: Option<u8> },
}

/// Parses SAN grammar only — doesn't know whether the resulting move is
/// legal, or even whether the named piece/square combination is
/// geometrically possible. Trailing check/mate markers (`+`, `#`) and
/// informal annotation glyphs (`!`, `?`, and combinations) are expected
/// to already be stripped by the caller ([`crate::pgn`] does this).
fn parse_san(token: &str) -> Option<SanMove> {
    match token {
        "O-O" | "0-0" => return Some(SanMove::CastleKingside),
        "O-O-O" | "0-0-0" => return Some(SanMove::CastleQueenside),
        _ => {}
    }

    let (body, promotion) = match token.find('=') {
        Some(idx) => {
            let promo_char = token[idx + 1..].chars().next()?;
            let (_, kind) = char_piece(promo_char)?;
            (&token[..idx], Some(kind))
        }
        None => (token, None),
    };

    let chars: Vec<char> = body.chars().collect();
    if chars.len() < 2 {
        return None;
    }
    let dest_str: String = chars[chars.len() - 2..].iter().collect();
    let dest = square_from_algebraic(&dest_str)?;
    let rest = &chars[..chars.len() - 2];

    let (piece, rest) = match rest.first() {
        Some(&c) if "KQRBN".contains(c) => (char_piece(c)?.1, &rest[1..]),
        _ => (PAWN, rest),
    };

    let mut from_file = None;
    let mut from_rank = None;
    for &c in rest {
        if c == 'x' {
            continue; // capture marker, not needed to resolve — legality/board state settles that
        }
        if c.is_ascii_lowercase() && ('a'..='h').contains(&c) {
            from_file = Some(c as u8 - b'a');
        } else if c.is_ascii_digit() {
            from_rank = Some(c as u8 - b'1');
        }
    }

    Some(SanMove::Normal { piece, dest, from_file, from_rank, promotion })
}

fn file_of(sq: u8) -> u8 {
    sq % 8
}
fn rank_of(sq: u8) -> u8 {
    sq / 8
}

/// Resolves one SAN token against `board`'s actual legal moves. `Err`
/// distinguishes "no legal move matches" from "more than one does" —
/// both are real parse failures (a malformed or out-of-context SAN
/// token), not the same failure for debugging purposes.
pub fn resolve(board: &Board, token: &str) -> Result<Move, String> {
    let parsed = parse_san(token).ok_or_else(|| format!("unparseable SAN: {token:?}"))?;
    let legal = movegen::legal_moves(board);

    let candidates: Vec<Move> = match &parsed {
        SanMove::CastleKingside | SanMove::CastleQueenside => {
            let want_diff: i32 = if matches!(parsed, SanMove::CastleKingside) { 2 } else { -2 };
            legal
                .into_iter()
                .filter(|m| {
                    board.piece_at(m.from).is_some_and(|(_, k)| k == KING)
                        && (m.to as i32 - m.from as i32) == want_diff
                })
                .collect()
        }
        SanMove::Normal { piece, dest, from_file, from_rank, promotion } => legal
            .into_iter()
            .filter(|m| {
                m.to == *dest
                    && board.piece_at(m.from).is_some_and(|(_, k)| k == *piece)
                    && from_file.is_none_or(|f| file_of(m.from) == f)
                    && from_rank.is_none_or(|r| rank_of(m.from) == r)
                    && m.promotion == *promotion
            })
            .collect(),
    };

    match candidates.len() {
        1 => Ok(candidates[0]),
        0 => Err(format!("no legal move matches SAN {token:?} in this position")),
        n => Err(format!("SAN {token:?} is ambiguous: {n} legal moves match")),
    }
}

pub fn move_to_uci(m: &Move) -> String {
    let mut s = format!("{}{}", square_to_algebraic(m.from), square_to_algebraic(m.to));
    if let Some(p) = m.promotion {
        s.push(match p {
            crate::chess::QUEEN => 'q',
            crate::chess::ROOK => 'r',
            crate::chess::BISHOP => 'b',
            crate::chess::KNIGHT => 'n',
            _ => 'q',
        });
    }
    s
}
