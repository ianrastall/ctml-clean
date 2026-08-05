//! Ties [`crate::pgn`] (tokenizing), [`crate::san`] (move resolution via
//! [`crate::movegen`]), and [`crate::fingerprint`] together: one parsed
//! PGN game in, one [`ParsedGame`] (UCI moves + both fingerprints) or a
//! clear error out. `game_xml` then renders that into the `ctml:game`
//! shape `xsd/ctml-game.xsd` defines — read directly before writing this,
//! same discipline as everywhere else this session (see `HANDOFF.md`).
//!
//! Field selection and the PGN `Date` parsing rule are a direct port of
//! `scripts/pgn_to_ctml.py`'s `ParsedGame`/`parse_pgn_date` — that script
//! is the reference this repo already has for what a CTML game record
//! should carry.

use crate::chess::Board;
use crate::pgn::PgnGame;
use crate::tournament::PartialDate;
use crate::xmlutil::{esc, normalize_space};

pub struct ParsedGame {
    pub event: String,
    pub site: String,
    pub date: Option<PartialDate>,
    pub round: String,
    pub white: String,
    pub black: String,
    pub white_elo: Option<i64>,
    pub black_elo: Option<i64>,
    pub result: String,
    pub eco: Option<String>,
    pub termination: Option<String>,
    pub start_fen: Option<String>,
    pub uci_moves: Vec<String>,
    pub trajectory_fp: String,
    pub final_position_fp: String,
}

/// PGN dates are `"YYYY.MM.DD"`, but month and/or day are routinely
/// `"??"` (unknown) — common enough in real databases that it's not an
/// edge case. Returns whatever precision the source actually supports;
/// `"????.??.??"` (fully unknown) is `None`. Ported from
/// `scripts/pgn_to_ctml.py::parse_pgn_date` field-for-field.
pub fn parse_pgn_date(raw: &str) -> Option<PartialDate> {
    let parts: Vec<&str> = raw.split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    let (y_s, mo_s, d_s) = (parts[0], parts[1], parts[2]);
    let is_digits = |s: &str, len: usize| s.len() == len && s.bytes().all(|b| b.is_ascii_digit());
    let y_ok = y_s == "????" || is_digits(y_s, 4);
    let mo_ok = mo_s == "??" || is_digits(mo_s, 2);
    let d_ok = d_s == "??" || is_digits(d_s, 2);
    if !(y_ok && mo_ok && d_ok) {
        return None;
    }
    if y_s == "????" {
        return None;
    }
    let y: i32 = y_s.parse().ok()?;

    if mo_s == "??" {
        return Some(PartialDate { y, m: None, d: None });
    }
    let mo: u32 = mo_s.parse().ok()?;
    if !(1..=12).contains(&mo) {
        return Some(PartialDate { y, m: None, d: None });
    }

    if d_s == "??" {
        return Some(PartialDate { y, m: Some(mo), d: None });
    }
    let d: u32 = d_s.parse().ok()?;
    let candidate = PartialDate { y, m: Some(mo), d: Some(d) };
    if !candidate.is_calendar_day() {
        return Some(PartialDate { y, m: Some(mo), d: None });
    }
    Some(candidate)
}

fn parse_elo(raw: &str) -> Option<i64> {
    let t = raw.trim();
    if !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit()) {
        t.parse().ok()
    } else {
        None
    }
}

const ECO_VALID: fn(&str) -> bool =
    |s| s.len() == 3 && matches!(s.as_bytes()[0], b'A'..=b'E') && s.as_bytes()[1..].iter().all(u8::is_ascii_digit);

fn map_termination(raw: &str) -> Option<String> {
    let mapped = match raw.trim().to_lowercase().as_str() {
        "normal" => "normal",
        "time forfeit" => "timeout",
        "abandoned" => "abandoned",
        "adjudication" => "adjudication",
        "rules infraction" => "forfeit",
        "unterminated" => "unknown",
        _ => return None,
    };
    Some(mapped.to_string())
}

#[derive(Debug)]
pub enum ImportError {
    San(String),
    BadFen,
}

impl std::fmt::Display for ImportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ImportError::San(msg) => write!(f, "{msg}"),
            ImportError::BadFen => write!(f, "unparseable start FEN"),
        }
    }
}

/// Replays one PGN game's mainline through the move generator, producing
/// UCI moves and both Zobrist fingerprints. `[SetUp "1"]` + `[FEN "..."]`
/// (a non-standard start — Chess960, an odds game, a puzzle continuing
/// from a mid-game position) is honored when present; otherwise the
/// standard starting position is assumed, matching `chess.pgn`'s own
/// default.
pub fn import_game(g: &PgnGame) -> Result<ParsedGame, ImportError> {
    let tag = |k: &str| g.tags.get(k).map(|s| normalize_space(s)).filter(|s| !s.is_empty());

    let start_fen = if tag("SetUp").as_deref() == Some("1") { tag("FEN") } else { None };
    let mut board = match &start_fen {
        Some(fen) => Board::from_fen(fen).ok_or(ImportError::BadFen)?,
        None => Board::starting_position(),
    };

    let mut uci_moves = Vec::with_capacity(g.sans.len());
    for san_token in &g.sans {
        let mv = crate::san::resolve(&board, san_token)
            .map_err(|e| ImportError::San(format!("{e} (game: {:?} vs {:?})", tag("White"), tag("Black"))))?;
        uci_moves.push(crate::san::move_to_uci(&mv));
        board.apply_move(mv.from, mv.to, mv.promotion);
    }

    let fp = crate::fingerprint::compute(&uci_moves, start_fen.as_deref())
        .expect("moves were already validated by san::resolve/apply_move above");

    let eco = tag("ECO").filter(|e| ECO_VALID(e));

    Ok(ParsedGame {
        event: tag("Event").unwrap_or_default(),
        site: tag("Site").unwrap_or_default(),
        date: tag("Date").and_then(|d| parse_pgn_date(&d)),
        round: tag("Round").unwrap_or_else(|| "?".to_string()),
        white: tag("White").unwrap_or_default(),
        black: tag("Black").unwrap_or_default(),
        white_elo: tag("WhiteElo").and_then(|e| parse_elo(&e)),
        black_elo: tag("BlackElo").and_then(|e| parse_elo(&e)),
        result: if matches!(g.result.as_str(), "1-0" | "0-1" | "1/2-1/2" | "0-0") { g.result.clone() } else { "*".to_string() },
        eco,
        termination: tag("Termination").and_then(|t| map_termination(&t)),
        start_fen,
        uci_moves,
        trajectory_fp: fp.trajectory,
        final_position_fp: fp.final_position,
    })
}

/// Renders one game as `<ctml:game>`, per `xsd/ctml-game.xsd`'s
/// `GameType` — element order matters (`xs:sequence`): `eco`, `start`,
/// `moves`, `termination`, `fingerprints`, `source`. `white_id`/
/// `black_id` are the enclosing tournament's `ctml:participant/@id`
/// values (`@white`/`@black` are `xs:IDREF`, resolved by the caller —
/// participant assignment is tournament-level, not this module's job).
pub fn game_xml(g: &ParsedGame, white_id: &str, black_id: &str, source_kind: &str, indent: &str) -> String {
    let mut lines = vec![format!(
        r#"{indent}<ctml:game round="{}" white="{white_id}" black="{black_id}" result="{}">"#,
        esc(&g.round),
        esc(&g.result)
    )];

    if let Some(eco) = &g.eco {
        lines.push(format!("{indent}  <ctml:eco>{eco}</ctml:eco>"));
    }
    if let Some(fen) = &g.start_fen {
        lines.push(format!("{indent}  <ctml:start standard=\"false\"><ctml:fen>{}</ctml:fen></ctml:start>", esc(fen)));
    }
    if !g.uci_moves.is_empty() {
        lines.push(format!(r#"{indent}  <ctml:moves notation="uci" plyCount="{}">"#, g.uci_moves.len()));
        for (i, mv) in g.uci_moves.iter().enumerate() {
            lines.push(format!(r#"{indent}    <ctml:move ply="{}" value="{mv}"/>"#, i + 1));
        }
        lines.push(format!("{indent}  </ctml:moves>"));
    }
    if let Some(term) = &g.termination {
        lines.push(format!("{indent}  <ctml:termination>{term}</ctml:termination>"));
    }
    lines.push(format!("{indent}  <ctml:fingerprints>"));
    lines.push(format!(
        r#"{indent}    <ctml:fingerprint scheme="zobrist-polyglot-1" scope="trajectory" value="{}"/>"#,
        g.trajectory_fp
    ));
    lines.push(format!(
        r#"{indent}    <ctml:fingerprint scheme="zobrist-polyglot-1" scope="finalPosition" value="{}"/>"#,
        g.final_position_fp
    ));
    lines.push(format!("{indent}  </ctml:fingerprints>"));
    lines.push(format!(r#"{indent}  <ctml:source kind="{}"/>"#, esc(source_kind)));
    lines.push(format!("{indent}</ctml:game>"));

    lines.join("\n")
}
