//! Legal move generation on top of [`crate::chess::Board`]. Needed for
//! PGN import: SAN notation (`"Nf3"`, `"exd5"`, `"O-O"`) only names a
//! destination and a piece kind, sometimes with a disambiguating file or
//! rank — resolving that to one `(from, to)` pair requires knowing every
//! *legal* move available in the position, not just "some piece of that
//! kind could reach that square" (a pinned piece, or a move that would
//! leave your own king in check, has to be excluded).
//!
//! Approach: generate pseudo-legal moves per piece (ignoring whether the
//! mover's own king ends up in check), then filter by actually applying
//! each candidate to a cloned board and checking. Simple and clearly
//! correct rather than fast (no pin-detection shortcut) — a deliberate
//! choice matching this project's "correctness first, profile once
//! there's a real workload" pattern elsewhere this session. Standard
//! chess only, no Chess960, matching `chess.rs`.

use crate::chess::{Board, BISHOP, KING, KNIGHT, PAWN, QUEEN, ROOK, WHITE};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Move {
    pub from: u8,
    pub to: u8,
    pub promotion: Option<u8>,
}

fn file(sq: u8) -> i32 {
    (sq % 8) as i32
}

fn rank(sq: u8) -> i32 {
    (sq / 8) as i32
}

fn sq_of(file: i32, rank: i32) -> Option<u8> {
    if (0..8).contains(&file) && (0..8).contains(&rank) {
        Some((rank * 8 + file) as u8)
    } else {
        None
    }
}

const KNIGHT_DELTAS: [(i32, i32); 8] =
    [(1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2)];
const KING_DELTAS: [(i32, i32); 8] =
    [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)];
const BISHOP_DIRS: [(i32, i32); 4] = [(1, 1), (1, -1), (-1, 1), (-1, -1)];
const ROOK_DIRS: [(i32, i32); 4] = [(1, 0), (-1, 0), (0, 1), (0, -1)];

/// Is `sq` attacked by any piece of `by_color`? Used both for check
/// detection and for castling's "king doesn't pass through/land on an
/// attacked square" rule.
pub fn is_square_attacked(board: &Board, sq: u8, by_color: u8) -> bool {
    let f = file(sq);
    let r = rank(sq);

    // Pawns: a `by_color` pawn attacks `sq` from one rank "behind" it
    // relative to that pawn's own forward direction.
    let pawn_dr = if by_color == WHITE { -1 } else { 1 };
    for df in [-1i32, 1i32] {
        if let Some(from) = sq_of(f + df, r + pawn_dr) {
            if let Some((c, k)) = board.piece_at(from) {
                if c == by_color && k == PAWN {
                    return true;
                }
            }
        }
    }

    for (df, dr) in KNIGHT_DELTAS {
        if let Some(from) = sq_of(f + df, r + dr) {
            if let Some((c, k)) = board.piece_at(from) {
                if c == by_color && k == KNIGHT {
                    return true;
                }
            }
        }
    }

    for (df, dr) in KING_DELTAS {
        if let Some(from) = sq_of(f + df, r + dr) {
            if let Some((c, k)) = board.piece_at(from) {
                if c == by_color && k == KING {
                    return true;
                }
            }
        }
    }

    for &(df, dr) in &BISHOP_DIRS {
        let (mut cf, mut cr) = (f, r);
        loop {
            cf += df;
            cr += dr;
            let Some(at) = sq_of(cf, cr) else { break };
            if let Some((c, k)) = board.piece_at(at) {
                if c == by_color && (k == BISHOP || k == QUEEN) {
                    return true;
                }
                break;
            }
        }
    }

    for &(df, dr) in &ROOK_DIRS {
        let (mut cf, mut cr) = (f, r);
        loop {
            cf += df;
            cr += dr;
            let Some(at) = sq_of(cf, cr) else { break };
            if let Some((c, k)) = board.piece_at(at) {
                if c == by_color && (k == ROOK || k == QUEEN) {
                    return true;
                }
                break;
            }
        }
    }

    false
}

pub fn is_in_check(board: &Board, color: u8) -> bool {
    match board.king_square(color) {
        Some(ks) => is_square_attacked(board, ks, 1 - color),
        None => false, // no king on the board at all: not a real game, but not "in check" either
    }
}

fn push_pawn_move(moves: &mut Vec<Move>, from: u8, to: u8, to_rank: i32, promo_rank: i32) {
    if to_rank == promo_rank {
        for &p in &[QUEEN, ROOK, BISHOP, KNIGHT] {
            moves.push(Move { from, to, promotion: Some(p) });
        }
    } else {
        moves.push(Move { from, to, promotion: None });
    }
}

fn add_castling_moves(board: &Board, king_from: u8, color: u8, moves: &mut Vec<Move>) {
    if is_in_check(board, color) {
        return; // can't castle out of check
    }
    let rights = board.castling_rights();
    let (ks_idx, qs_idx, rank_base): (usize, usize, u8) =
        if color == WHITE { (0, 1, 0) } else { (2, 3, 56) };
    let opponent = 1 - color;

    if rights[ks_idx] {
        let (f, g, h) = (rank_base + 5, rank_base + 6, rank_base + 7);
        let rook_ok = matches!(board.piece_at(h), Some((c, ROOK)) if c == color);
        if board.piece_at(f).is_none()
            && board.piece_at(g).is_none()
            && rook_ok
            && !is_square_attacked(board, f, opponent)
            && !is_square_attacked(board, g, opponent)
        {
            moves.push(Move { from: king_from, to: g, promotion: None });
        }
    }
    if rights[qs_idx] {
        let (b, c_sq, d, a) = (rank_base + 1, rank_base + 2, rank_base + 3, rank_base);
        let rook_ok = matches!(board.piece_at(a), Some((c, ROOK)) if c == color);
        if board.piece_at(b).is_none()
            && board.piece_at(c_sq).is_none()
            && board.piece_at(d).is_none()
            && rook_ok
            && !is_square_attacked(board, d, opponent)
            && !is_square_attacked(board, c_sq, opponent)
        {
            moves.push(Move { from: king_from, to: c_sq, promotion: None });
        }
    }
}

/// Every move that obeys piece-movement rules, *without* checking
/// whether it leaves the mover's own king in check. [`legal_moves`]
/// filters this down.
pub fn pseudo_legal_moves(board: &Board) -> Vec<Move> {
    let color = board.turn();
    let opponent = 1 - color;
    let mut moves = Vec::new();

    for from in 0u8..64 {
        let Some((c, kind)) = board.piece_at(from) else { continue };
        if c != color {
            continue;
        }
        let (ff, fr) = (file(from), rank(from));

        match kind {
            PAWN => {
                let dir: i32 = if color == WHITE { 1 } else { -1 };
                let start_rank = if color == WHITE { 1 } else { 6 };
                let promo_rank = if color == WHITE { 7 } else { 0 };

                if let Some(to) = sq_of(ff, fr + dir) {
                    if board.piece_at(to).is_none() {
                        push_pawn_move(&mut moves, from, to, fr + dir, promo_rank);
                        if fr == start_rank {
                            if let Some(to2) = sq_of(ff, fr + 2 * dir) {
                                if board.piece_at(to2).is_none() {
                                    moves.push(Move { from, to: to2, promotion: None });
                                }
                            }
                        }
                    }
                }
                for df in [-1i32, 1i32] {
                    let Some(to) = sq_of(ff + df, fr + dir) else { continue };
                    if let Some((oc, _)) = board.piece_at(to) {
                        if oc == opponent {
                            push_pawn_move(&mut moves, from, to, fr + dir, promo_rank);
                        }
                    } else if Some(to) == board.ep_square() {
                        moves.push(Move { from, to, promotion: None });
                    }
                }
            }
            KNIGHT => {
                for (df, dr) in KNIGHT_DELTAS {
                    if let Some(to) = sq_of(ff + df, fr + dr) {
                        match board.piece_at(to) {
                            None => moves.push(Move { from, to, promotion: None }),
                            Some((oc, _)) if oc == opponent => moves.push(Move { from, to, promotion: None }),
                            _ => {}
                        }
                    }
                }
            }
            KING => {
                for (df, dr) in KING_DELTAS {
                    if let Some(to) = sq_of(ff + df, fr + dr) {
                        match board.piece_at(to) {
                            None => moves.push(Move { from, to, promotion: None }),
                            Some((oc, _)) if oc == opponent => moves.push(Move { from, to, promotion: None }),
                            _ => {}
                        }
                    }
                }
                add_castling_moves(board, from, color, &mut moves);
            }
            BISHOP | ROOK | QUEEN => {
                let dirs: &[(i32, i32)] = match kind {
                    BISHOP => &BISHOP_DIRS,
                    ROOK => &ROOK_DIRS,
                    _ => &[
                        (1, 1),
                        (1, -1),
                        (-1, 1),
                        (-1, -1),
                        (1, 0),
                        (-1, 0),
                        (0, 1),
                        (0, -1),
                    ],
                };
                for &(df, dr) in dirs {
                    let (mut cf, mut cr) = (ff, fr);
                    loop {
                        cf += df;
                        cr += dr;
                        let Some(to) = sq_of(cf, cr) else { break };
                        match board.piece_at(to) {
                            None => moves.push(Move { from, to, promotion: None }),
                            Some((oc, _)) => {
                                if oc == opponent {
                                    moves.push(Move { from, to, promotion: None });
                                }
                                break;
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }

    moves
}

/// Pseudo-legal moves filtered to exclude any that leave the mover's own
/// king in check — the actual legality rule. Correctness over speed: a
/// clone-and-check per candidate, not incremental pin/check tracking.
pub fn legal_moves(board: &Board) -> Vec<Move> {
    let color = board.turn();
    pseudo_legal_moves(board)
        .into_iter()
        .filter(|m| {
            let mut after = board.clone();
            after.apply_move(m.from, m.to, m.promotion);
            !is_in_check(&after, color)
        })
        .collect()
}

/// Standard move-generator correctness check: count leaf nodes reachable
/// in exactly `depth` plies. Compared against `python-chess`'s own
/// `board.legal_moves` counts for known positions — see
/// `movegen-selftest` — because a hand-derived expected count is exactly
/// the kind of thing worth cross-checking against a trusted independent
/// implementation rather than trusting either one alone.
pub fn perft(board: &Board, depth: u32) -> u64 {
    if depth == 0 {
        return 1;
    }
    let moves = legal_moves(board);
    if depth == 1 {
        return moves.len() as u64;
    }
    let mut nodes = 0u64;
    for m in moves {
        let mut after = board.clone();
        after.apply_move(m.from, m.to, m.promotion);
        nodes += perft(&after, depth - 1);
    }
    nodes
}
