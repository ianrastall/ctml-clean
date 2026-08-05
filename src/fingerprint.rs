//! `spec/fingerprint.md`'s `zobrist-polyglot-1` scheme: `trajectory`
//! (SHA-256 over every position's Zobrist bytes, ply 0 through the last
//! move, in order) and `finalPosition` (the last ply's raw Zobrist value,
//! hex-encoded, no second hash layer). Both scopes share one ply-by-ply
//! walk — see the spec's own reference pseudocode, which this is a direct
//! translation of.

use crate::chess::Board;
use sha2::{Digest, Sha256};

pub struct Fingerprints {
    /// 64 hex chars.
    pub trajectory: String,
    /// 16 hex chars.
    pub final_position: String,
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// `moves` are UCI (`"e2e4"`, `"e7e8q"`, ...), already legal — this
/// applies them without checking. `start_fen` defaults to the standard
/// starting position when `None`.
pub fn compute(moves: &[String], start_fen: Option<&str>) -> Option<Fingerprints> {
    let mut board = match start_fen {
        Some(fen) => Board::from_fen(fen)?,
        None => Board::starting_position(),
    };

    let mut hasher = Sha256::new();
    let mut zb = board.zobrist_hash().to_be_bytes();
    hasher.update(zb); // ply 0, the starting position — see spec: never skip this.

    for mv in moves {
        board.apply_uci(mv)?;
        zb = board.zobrist_hash().to_be_bytes();
        hasher.update(zb);
    }

    let trajectory = hex_lower(&hasher.finalize());
    let final_position = hex_lower(&zb);

    Some(Fingerprints { trajectory, final_position })
}
