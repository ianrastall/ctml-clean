//! PGN tokenizer: splits a PGN file into games, each with its tag-pair
//! map and a flat list of mainline SAN tokens (move numbers, comments,
//! NAGs, and variations are consumed and discarded here — matching
//! `scripts/pgn_to_ctml.py`'s own scope, which only ever extracts
//! `game.mainline_moves()` and stores nothing else from the movetext).
//! [`crate::san`] resolves each SAN token to an actual move using
//! [`crate::movegen`]; this module doesn't know what a legal move is.

use std::collections::HashMap;

pub struct PgnGame {
    pub tags: HashMap<String, String>,
    /// Mainline SAN tokens in order, e.g. `["e4", "e5", "Nf3", ...]` —
    /// move-number markers, comments, NAGs, and variations already
    /// stripped out.
    pub sans: Vec<String>,
    /// `"1-0"`, `"0-1"`, `"1/2-1/2"`, or `"*"`.
    pub result: String,
}

struct Scanner {
    chars: Vec<char>,
    pos: usize,
}

impl Scanner {
    fn new(s: &str) -> Self {
        Scanner { chars: s.chars().collect(), pos: 0 }
    }
    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }
    fn advance(&mut self) -> Option<char> {
        let c = self.peek();
        if c.is_some() {
            self.pos += 1;
        }
        c
    }
    fn eof(&self) -> bool {
        self.pos >= self.chars.len()
    }
    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(c) if c.is_whitespace()) {
            self.pos += 1;
        }
    }
}

/// Parses one `[Key "Value"]` tag pair, handling PGN's `\"` / `\\`
/// escapes inside the quoted value. Assumes the scanner is positioned at
/// (or before, across whitespace) the opening `[`.
fn parse_tag(sc: &mut Scanner) -> Option<(String, String)> {
    sc.skip_ws();
    if sc.peek() != Some('[') {
        return None;
    }
    sc.advance();
    sc.skip_ws();

    let mut key = String::new();
    while matches!(sc.peek(), Some(c) if c.is_alphanumeric() || c == '_') {
        key.push(sc.advance().unwrap());
    }
    sc.skip_ws();

    let mut value = String::new();
    if sc.peek() == Some('"') {
        sc.advance();
        loop {
            match sc.advance() {
                None => break,
                Some('"') => break,
                Some('\\') => {
                    if let Some(escaped) = sc.advance() {
                        value.push(escaped);
                    }
                }
                Some(c) => value.push(c),
            }
        }
    }
    // Recover to the closing `]` regardless of whether the value parsed
    // cleanly, so one malformed tag doesn't desync the rest of the file.
    while matches!(sc.peek(), Some(c) if c != ']') {
        sc.advance();
    }
    sc.advance();

    Some((key, value))
}

fn is_result_token(s: &str) -> bool {
    matches!(s, "1-0" | "0-1" | "1/2-1/2" | "*")
}

/// Strips a leading move-number marker (`"12."`, `"12..."`) off one raw
/// movetext token, e.g. `"1.e4"` -> `"e4"`, `"23...Nf6"` -> `"Nf6"`,
/// `"1."` -> `""` (a bare move number, nothing else — caller drops it).
/// Deliberately conservative: `"1-0"` / `"1/2-1/2"` have leading digits
/// with no following dot, so they pass through unchanged rather than
/// being mistaken for move numbers.
fn strip_move_number(tok: &str) -> &str {
    let chars: Vec<char> = tok.chars().collect();
    let mut i = 0;
    while i < chars.len() && chars[i].is_ascii_digit() {
        i += 1;
    }
    if i == 0 {
        return tok;
    }
    let mut j = i;
    while j < chars.len() && chars[j] == '.' {
        j += 1;
    }
    if j == i {
        return tok; // digits with no following dot: not a move-number token
    }
    let byte_offset: usize = chars[..j].iter().map(|c| c.len_utf8()).sum();
    &tok[byte_offset..]
}

/// Consumes movetext up to (and including) the result token, or EOF, or
/// the start of the next game's tags — returns the mainline SAN tokens
/// and the result string.
fn scan_movetext(sc: &mut Scanner) -> (Vec<String>, String) {
    let mut sans = Vec::new();
    let mut result = "*".to_string();

    loop {
        sc.skip_ws();
        match sc.peek() {
            None | Some('[') => break,
            Some('{') => {
                sc.advance();
                while sc.peek().is_some() && sc.peek() != Some('}') {
                    sc.advance();
                }
                sc.advance();
            }
            Some(';') => {
                while sc.peek().is_some() && sc.peek() != Some('\n') {
                    sc.advance();
                }
            }
            Some('(') => {
                let mut depth = 0i32;
                loop {
                    match sc.advance() {
                        Some('(') => depth += 1,
                        Some(')') => {
                            depth -= 1;
                            if depth == 0 {
                                break;
                            }
                        }
                        None => break,
                        _ => {}
                    }
                }
            }
            Some('$') => {
                sc.advance();
                while matches!(sc.peek(), Some(c) if c.is_ascii_digit()) {
                    sc.advance();
                }
            }
            Some(_) => {
                let mut tok = String::new();
                while let Some(c) = sc.peek() {
                    if c.is_whitespace() || "{}();$[]".contains(c) {
                        break;
                    }
                    tok.push(c);
                    sc.advance();
                }
                if tok.is_empty() {
                    sc.advance(); // safety: don't spin forever on an unexpected char
                    continue;
                }
                let stripped = strip_move_number(&tok);
                if stripped.is_empty() {
                    continue;
                }
                if is_result_token(stripped) {
                    result = stripped.to_string();
                    break;
                }
                // Trailing check/mate markers (`+`, `#`) and informal
                // annotation glyphs (`!`, `?`, `!!`, ...) some PGN
                // writers embed directly rather than as a `$n` NAG —
                // none of these are part of SAN's actual grammar for
                // resolving a move, just commentary glued onto the token.
                let san = stripped.trim_end_matches(['!', '?', '+', '#']).to_string();
                if !san.is_empty() {
                    sans.push(san);
                }
            }
        }
    }

    (sans, result)
}

pub fn parse_games(text: &str) -> Vec<PgnGame> {
    let mut sc = Scanner::new(text);
    let mut games = Vec::new();

    loop {
        sc.skip_ws();
        if sc.eof() {
            break;
        }

        let mut tags = HashMap::new();
        loop {
            sc.skip_ws();
            if sc.peek() == Some('[') {
                if let Some((k, v)) = parse_tag(&mut sc) {
                    tags.insert(k, v);
                }
            } else {
                break;
            }
        }

        let (sans, result) = scan_movetext(&mut sc);
        if tags.is_empty() && sans.is_empty() {
            break; // nothing parsed this round: avoid spinning on trailing garbage
        }
        games.push(PgnGame { tags, sans, result });
    }

    games
}
