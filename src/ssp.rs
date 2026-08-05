//! `assets/ratings260801.ssp`: the source-of-truth player/rating file this
//! project's XML player registry is generated from (`source=` attribute on
//! `ctml:playerRegistry` names the sibling `.ssp` snapshot it came from).
//! Custom line-oriented format, ~10M lines / 493MB, four sections marked by
//! `@PLAYER` / `@EVENT` / `@SITE` / `@ROUND` header lines:
//!
//! - `@PLAYER`: one unindented "name line" per player
//!   (`Name, Given #title FED [rating] birthdate`), followed by indented
//!   detail lines (`%Bio`, `%Elo <year>:...`, `= <alias>`) until the next
//!   blank line.
//! - `@EVENT` / `@SITE` / `@ROUND`: name-normalization rule tables
//!   (`%Prefix`/`%Infix`/`%Suffix "from" "to"`), not per-record data —
//!   there is no per-event or per-site data in this file, despite the
//!   section names.
//!
//! This is a byte-level scan (no `String` allocation per line) because at
//! this size a naive `BufRead::lines()` pass shows up as real wall-clock
//! time; see `scan()`'s timing in `ctml-clean stats`.

use crate::names::split_name;
use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

#[derive(Default, Debug)]
pub struct SspStats {
    pub lines: u64,
    pub bytes: u64,
    pub player_records: u64,
    pub player_aliases: u64,
    pub player_elo_lines: u64,
    pub player_fide_ids: u64,
    pub event_rules: u64,
    pub site_rules: u64,
    pub round_rules: u64,
    pub comment_lines: u64,
}

#[derive(Clone, Copy, PartialEq)]
enum Section {
    None,
    Player,
    Event,
    Site,
    Round,
}

pub fn scan(path: &Path) -> std::io::Result<SspStats> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut stats = SspStats::default();
    let mut section = Section::None;
    let mut buf: Vec<u8> = Vec::with_capacity(256);

    loop {
        buf.clear();
        let n = reader.read_until(b'\n', &mut buf)?;
        if n == 0 {
            break;
        }
        stats.lines += 1;
        stats.bytes += n as u64;

        let mut line: &[u8] = &buf;
        while matches!(line.last(), Some(b'\n' | b'\r')) {
            line = &line[..line.len() - 1];
        }
        let indented = line.first().is_some_and(|b| *b == b' ' || *b == b'\t');
        let trimmed = trim_ascii(line);

        if trimmed.is_empty() {
            continue;
        }

        if trimmed[0] == b'@' {
            section = if trimmed.starts_with(b"@PLAYER") {
                Section::Player
            } else if trimmed.starts_with(b"@EVENT") {
                Section::Event
            } else if trimmed.starts_with(b"@SITE") {
                Section::Site
            } else if trimmed.starts_with(b"@ROUND") {
                Section::Round
            } else {
                Section::None
            };
            continue;
        }

        // Top-level `#`-prefixed comment, e.g. the `build_enriched_ssp.py`
        // banner wrapping the CTML-generated normalization tables at the
        // end of the file. Not indented, so it would otherwise be
        // misclassified as a same-section record line (a real bug this
        // parser had until the stats/registry cross-check caught it: it
        // was inflating `player_records` by exactly the 3 comment lines
        // that land inside `@PLAYER` before the first `@SITE` header).
        if !indented && trimmed[0] == b'#' {
            stats.comment_lines += 1;
            continue;
        }

        match section {
            Section::Player if indented => {
                if trimmed.starts_with(b"= ") {
                    stats.player_aliases += 1;
                } else if trimmed.starts_with(b"%Elo") {
                    stats.player_elo_lines += 1;
                } else if trimmed.starts_with(b"%Bio") && contains(trimmed, b"FIDE") {
                    stats.player_fide_ids += 1;
                }
            }
            Section::Player => stats.player_records += 1,
            Section::Event => stats.event_rules += 1,
            Section::Site => stats.site_rules += 1,
            Section::Round => stats.round_rules += 1,
            Section::None => {}
        }
    }

    Ok(stats)
}

fn trim_ascii(mut s: &[u8]) -> &[u8] {
    while matches!(s.first(), Some(b) if b.is_ascii_whitespace()) {
        s = &s[1..];
    }
    while matches!(s.last(), Some(b) if b.is_ascii_whitespace()) {
        s = &s[..s.len() - 1];
    }
    s
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

// ---------------------------------------------------------------------
// Typed parse, for diffing against the XML player registry
// (`crate::xmlplayers`). `scan()` above stays as the cheap byte-level
// counter `stats` uses; this builds real owned records and so costs more
// time and memory, which is fine for a one-shot diff run.
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct SspName {
    pub display: String,
    pub family: String,
    pub given: Vec<String>,
    pub suffix: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SspPlayer {
    pub name: SspName,
    /// Recognized titles only, in the order their tokens appeared,
    /// deduplicated — see [`parse_title_token`]. Repeatable because the
    /// schema is (`ctml-entities.xsd`, `title` `maxOccurs="unbounded"`,
    /// added specifically for tokens like `IM+WGM`).
    pub titles: Vec<String>,
    /// From a `W`-prefixed title subtoken (`W`, `WC`, `WCM`, `WF`, `WFM`,
    /// `WGM`, `WIM` all count, recognized or not) — a gender signal
    /// independent of whether any title was recognized. Maps to
    /// `<ctml:sex>F</ctml:sex>`.
    pub female: bool,
    pub federation: Option<String>,
    /// The `[NNNN]` token on the name line. **Not** the same thing as the
    /// registry's `<current>` rating — confirmed by diffing against real
    /// data: `<current>` tracks the last non-`?` month in `rating_history`
    /// below, which for a large fraction of players (roughly 70% of those
    /// compared) is a different value than this one, e.g. FIDE id
    /// 16234243 carries `[1960]` here while its own December-2025 history
    /// entry — and the registry's `<current>` — is 1958. Kept anyway
    /// because it's real data straight off the name line, not because
    /// it's the "right" current rating.
    pub bracket_rating: Option<i32>,
    pub birth_year: Option<i32>,
    pub aliases: Vec<SspName>,
    pub rating_history: BTreeMap<i32, [Option<i32>; 12]>,
}

/// The last non-`?` month in `history`, walking chronologically —
/// verified against the registry's own `<current>` element, which this
/// matches exactly where `bracket_rating` above does not.
pub fn latest_rating(history: &BTreeMap<i32, [Option<i32>; 12]>) -> Option<i32> {
    history
        .iter()
        .rev()
        .find_map(|(_, months)| months.iter().rev().find_map(|v| *v))
}

#[derive(Default)]
pub struct ParsePlayersResult {
    /// Keyed by FIDE id — the only reliable join key against the XML side.
    pub by_fide_id: HashMap<u64, SspPlayer>,
    pub without_fide_id: u64,
    pub malformed_name_lines: u64,
    pub comment_lines: u64,
}

/// Recognized `PlayerTitleType` codes — a title subtoken not in this set
/// (e.g. `WC`, `WF`, `HM`, `WH`) is dropped, not guessed at; matches
/// `RECOGNIZED_TITLES` in `scripts/ssp_to_ctml_players.py` exactly.
const RECOGNIZED_TITLES: &[&str] = &["GM", "IM", "FM", "CM", "WGM", "WIM", "WFM", "WCM", "NM"];

/// Splits a `+`-joined title token (e.g. `"IM+WGM"`, bare `"-"`, bare
/// `"W"`) into `(recognized titles in order, female)`, replicating
/// `parse_title_token` in `scripts/ssp_to_ctml_players.py`. `female` is
/// set by *any* `W`-prefixed subtoken, recognized or not — it's a
/// broader signal than "has a women's title".
fn parse_title_token(token: &str) -> (Vec<String>, bool) {
    let mut titles = Vec::new();
    let mut female = false;
    for sub in token.split('+') {
        if sub.is_empty() || sub == "-" {
            continue;
        }
        if sub.starts_with('W') {
            female = true;
        }
        if RECOGNIZED_TITLES.contains(&sub) && !titles.iter().any(|t: &String| t == sub) {
            titles.push(sub.to_string());
        }
    }
    (titles, female)
}

fn looks_like_fed(tok: &str) -> bool {
    tok.len() == 3 && tok.bytes().all(|b| b.is_ascii_uppercase())
}

fn looks_like_rating(tok: &str) -> bool {
    tok.len() >= 2 && tok.starts_with('[') && tok.ends_with(']')
}

fn looks_like_date(tok: &str) -> bool {
    let b = tok.as_bytes();
    b.len() == 10
        && b[4] == b'.'
        && b[7] == b'.'
        && b[..4].iter().all(|c| c.is_ascii_digit() || *c == b'?')
        && b[5..7].iter().all(|c| c.is_ascii_digit() || *c == b'?')
        && b[8..10].iter().all(|c| c.is_ascii_digit() || *c == b'?')
}

fn parse_rating_token(tok: &str) -> Option<i32> {
    tok[1..tok.len() - 1].parse().ok()
}

fn parse_year_token(tok: &str) -> Option<i32> {
    tok[..4].parse().ok()
}

type NameLine = (String, Vec<String>, bool, Option<String>, Option<i32>, Option<i32>);

/// Parses one unindented name-line: `<name...> #<title> [FED] [rating]
/// [birthdate]`. Verified against the whole file first (not guessed):
/// title and rating are always present; FED and the birthdate are each
/// independently optional but, when present, always appear in that
/// relative order. Returns `None` (and the caller counts it as malformed
/// rather than guessing) for anything outside those four shapes.
fn parse_name_line(line: &str) -> Option<NameLine> {
    let tokens: Vec<&str> = line.split_whitespace().collect();
    let title_idx = tokens.iter().position(|t| t.starts_with('#'))?;
    let name = tokens[..title_idx].join(" ");
    let (titles, female) = parse_title_token(&tokens[title_idx][1..]);

    let rest = &tokens[title_idx + 1..];
    let (fed, rating, birth_year) = match rest {
        [fed, rating, date] if looks_like_fed(fed) && looks_like_rating(rating) && looks_like_date(date) => {
            (Some((*fed).to_string()), parse_rating_token(rating), parse_year_token(date))
        }
        [fed, rating] if looks_like_fed(fed) && looks_like_rating(rating) => {
            (Some((*fed).to_string()), parse_rating_token(rating), None)
        }
        [rating, date] if looks_like_rating(rating) && looks_like_date(date) => {
            (None, parse_rating_token(rating), parse_year_token(date))
        }
        [rating] if looks_like_rating(rating) => (None, parse_rating_token(rating), None),
        _ => return None,
    };

    Some((name, titles, female, fed, rating, birth_year))
}

pub fn parse_players(path: &Path) -> std::io::Result<ParsePlayersResult> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut out = ParsePlayersResult::default();
    let mut section = Section::None;
    let mut buf: Vec<u8> = Vec::with_capacity(256);

    let mut current: Option<SspPlayer> = None;
    let mut current_fide_id: Option<u64> = None;

    macro_rules! flush {
        () => {
            if let Some(p) = current.take() {
                match current_fide_id.take() {
                    Some(id) => {
                        out.by_fide_id.insert(id, p);
                    }
                    None => out.without_fide_id += 1,
                }
            }
        };
    }

    loop {
        buf.clear();
        let n = reader.read_until(b'\n', &mut buf)?;
        if n == 0 {
            break;
        }
        let mut line: &[u8] = &buf;
        while matches!(line.last(), Some(b'\n' | b'\r')) {
            line = &line[..line.len() - 1];
        }
        let indented = line.first().is_some_and(|b| *b == b' ' || *b == b'\t');
        let text = String::from_utf8_lossy(line);
        let trimmed = text.trim();

        if trimmed.is_empty() {
            continue;
        }

        if let Some(rest) = trimmed.strip_prefix('@') {
            flush!();
            section = if rest.starts_with("PLAYER") {
                Section::Player
            } else if rest.starts_with("EVENT") {
                Section::Event
            } else if rest.starts_with("SITE") {
                Section::Site
            } else if rest.starts_with("ROUND") {
                Section::Round
            } else {
                Section::None
            };
            continue;
        }

        if !indented && trimmed.starts_with('#') {
            out.comment_lines += 1;
            continue;
        }

        if section != Section::Player {
            continue;
        }

        if !indented {
            flush!();
            match parse_name_line(trimmed) {
                Some((name, titles, female, federation, bracket_rating, birth_year)) => {
                    let (family, given, suffix) = split_name(&name);
                    current = Some(SspPlayer {
                        name: SspName { display: name, family, given, suffix },
                        titles,
                        female,
                        federation,
                        bracket_rating,
                        birth_year,
                        aliases: Vec::new(),
                        rating_history: BTreeMap::new(),
                    });
                }
                None => out.malformed_name_lines += 1,
            }
            continue;
        }

        let Some(player) = current.as_mut() else { continue };

        if let Some(rest) = trimmed.strip_prefix("= ") {
            let (family, given, suffix) = split_name(rest);
            player.aliases.push(SspName { display: rest.to_string(), family, given, suffix });
        } else if let Some(rest) = trimmed.strip_prefix("%Bio ") {
            let mut parts = rest.splitn(2, ' ');
            if parts.next() == Some("FIDE") {
                // `ctml:FideIdType` requires 4-12 digits (xsd/ctml-vocab.xsd);
                // the generator routes anything shorter to `internalId`
                // instead (`scripts/ssp_to_ctml_players.py`'s
                // `VALID_FIDE_ID_RE`) rather than treat it as a real FIDE
                // id. One record in this file, "%Bio FIDE 19", is exactly
                // that case — without this check it was the one player
                // this parser had a FIDE-keyed record for that the
                // registry doesn't.
                let id_str = parts.next().map(str::trim).unwrap_or("");
                current_fide_id = if (4..=12).contains(&id_str.len()) && id_str.bytes().all(|b| b.is_ascii_digit())
                {
                    id_str.parse().ok()
                } else {
                    None
                };
            }
        } else if let Some(rest) = trimmed.strip_prefix("%Elo ") {
            if let Some((year_str, values_str)) = rest.split_once(':') {
                if let Ok(year) = year_str.parse::<i32>() {
                    let mut months = [None; 12];
                    for (i, v) in values_str.split(',').enumerate().take(12) {
                        if v != "?" {
                            months[i] = v.parse().ok();
                        }
                    }
                    player.rating_history.insert(year, months);
                }
            }
        }
    }
    flush!();

    Ok(out)
}
