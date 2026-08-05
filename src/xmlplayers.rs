//! Typed, streaming parser for `ctml:player` records out of
//! `assets/registries/players/players-*.xml`, built specifically to be
//! diffed against [`crate::ssp`]'s parse of the source `.ssp` file.
//!
//! Unlike [`crate::registry::scan_players_dir`] (which only counts), this
//! builds a full record per player: name, federation, title, FIDE id,
//! birth year, aliases, and rating history including the derived `peak`.
//! It's a manual stack-based pull parser rather than `serde`'s XML support
//! because several element local names repeat at different nesting depths
//! with different meaning (`family`/`given` under both `name` and
//! `aliases/alias`; `year` under both `birthDate` and `.../history`), and a
//! stack of recognized tags is the simplest way to disambiguate that
//! without over-fitting a derive to this one schema shape.

use quick_xml::events::{BytesStart, Event};
use quick_xml::Reader;
use std::collections::BTreeMap;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

#[derive(Debug, Clone, Default)]
pub struct XmlName {
    pub display: Option<String>,
    pub family: String,
    pub given: Vec<String>,
    pub suffix: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct XmlPlayer {
    pub ref_attr: String,
    pub name: XmlName,
    pub federation: Option<String>,
    /// Repeatable in the schema for exactly this reason — see
    /// `ctml-entities.xsd`'s comment on `title`, `maxOccurs="unbounded"`,
    /// added once source data turned up combined tokens like `IM+WGM`.
    pub titles: Vec<String>,
    pub female: bool,
    pub fide_id: Option<u64>,
    pub birth_year: Option<i32>,
    pub aliases: Vec<XmlName>,
    pub rating_system: Option<String>,
    pub current_rating: Option<i32>,
    pub peak_value: Option<i32>,
    pub peak_year: Option<i32>,
    pub peak_month: Option<u32>,
    pub rating_history: BTreeMap<i32, [Option<i32>; 12]>,
}

#[derive(Default)]
pub struct ParsePlayersResult {
    /// Keyed by FIDE id — the only reliable join key against the SSP side.
    pub by_fide_id: std::collections::HashMap<u64, XmlPlayer>,
    pub without_fide_id: u64,
    pub shards: usize,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Tag {
    Player,
    Name,
    Federation,
    Title,
    Ids,
    FideId,
    BirthDate,
    Year,
    Aliases,
    Alias,
    Family,
    Given,
    Suffix,
    Sex,
    RatingHistory,
    RatingTrack,
    Current,
    Peak,
    Value,
    Achieved,
    Month,
    History,
    Other,
}

fn tag_of(local: &[u8]) -> Tag {
    match local {
        b"player" => Tag::Player,
        b"name" => Tag::Name,
        b"federation" => Tag::Federation,
        b"title" => Tag::Title,
        b"ids" => Tag::Ids,
        b"fideId" => Tag::FideId,
        b"birthDate" => Tag::BirthDate,
        b"year" => Tag::Year,
        b"aliases" => Tag::Aliases,
        b"alias" => Tag::Alias,
        b"family" => Tag::Family,
        b"given" => Tag::Given,
        b"suffix" => Tag::Suffix,
        b"sex" => Tag::Sex,
        b"ratingHistory" => Tag::RatingHistory,
        b"ratingTrack" => Tag::RatingTrack,
        b"current" => Tag::Current,
        b"peak" => Tag::Peak,
        b"value" => Tag::Value,
        b"achieved" => Tag::Achieved,
        b"month" => Tag::Month,
        b"history" => Tag::History,
        _ => Tag::Other,
    }
}

fn attr(e: &BytesStart, key: &[u8]) -> Option<String> {
    e.attributes()
        .flatten()
        .find(|a| a.key.as_ref() == key)
        .and_then(|a| a.unescape_value().ok())
        .map(|c| c.into_owned())
}

/// Holds the in-progress state for one shard file. Every `Start`/`Empty`
/// element runs `enter`; every `End`/`Empty` element runs `exit` — `Empty`
/// runs both back to back immediately, since it never gets a separate
/// `End` event and must not be pushed onto `stack`.
struct FileParser<'o> {
    out: &'o mut ParsePlayersResult,
    stack: Vec<Tag>,
    player: Option<XmlPlayer>,
    cur_alias: Option<XmlName>,
    cur_history_year: Option<i32>,
    cur_history_month: Option<u32>,
}

impl<'o> FileParser<'o> {
    fn new(out: &'o mut ParsePlayersResult) -> Self {
        Self {
            out,
            stack: Vec::with_capacity(8),
            player: None,
            cur_alias: None,
            cur_history_year: None,
            cur_history_month: None,
        }
    }

    fn enter(&mut self, tag: Tag, e: &BytesStart) {
        let parent = self.stack.last().copied();
        match tag {
            Tag::Player => {
                self.player = Some(XmlPlayer {
                    ref_attr: attr(e, b"ref").unwrap_or_default(),
                    ..Default::default()
                });
            }
            Tag::Name if parent == Some(Tag::Player) => {
                if let Some(p) = self.player.as_mut() {
                    p.name.display = attr(e, b"display");
                }
            }
            Tag::Alias if parent == Some(Tag::Aliases) => {
                self.cur_alias = Some(XmlName {
                    display: attr(e, b"display"),
                    family: String::new(),
                    given: Vec::new(),
                    suffix: None,
                });
            }
            Tag::Year if parent == Some(Tag::BirthDate) => {
                if let Some(p) = self.player.as_mut() {
                    p.birth_year = attr(e, b"y").and_then(|s| s.parse().ok());
                }
            }
            Tag::Year if parent == Some(Tag::History) => {
                self.cur_history_year = attr(e, b"value").and_then(|s| s.parse().ok());
            }
            // `<history><year value="Y"><month num="N">...</month></year></history>`
            // — a history month's structural parent is `year`, not
            // `history` directly. Getting this wrong meant
            // `cur_history_month` was never set and every player's
            // parsed `rating_history` came out empty (100% "mismatch"
            // against the SSP side on first run — the giveaway that this
            // was a parser bug, not a data one).
            Tag::Month if parent == Some(Tag::Year) => {
                self.cur_history_month = attr(e, b"num").and_then(|s| s.parse().ok());
            }
            Tag::Month if parent == Some(Tag::Achieved) => {
                if let Some(p) = self.player.as_mut() {
                    p.peak_year = attr(e, b"y").and_then(|s| s.parse().ok());
                    p.peak_month = attr(e, b"m").and_then(|s| s.parse().ok());
                }
            }
            Tag::RatingTrack if parent == Some(Tag::RatingHistory) => {
                if let Some(p) = self.player.as_mut() {
                    p.rating_system = attr(e, b"system");
                }
            }
            _ => {}
        }
        self.stack.push(tag);
    }

    fn exit(&mut self) {
        let Some(tag) = self.stack.pop() else { return };
        match tag {
            Tag::Player => {
                if let Some(p) = self.player.take() {
                    match p.fide_id {
                        Some(id) => {
                            self.out.by_fide_id.insert(id, p);
                        }
                        None => self.out.without_fide_id += 1,
                    }
                }
            }
            Tag::Alias => {
                if let (Some(a), Some(p)) = (self.cur_alias.take(), self.player.as_mut()) {
                    p.aliases.push(a);
                }
            }
            _ => {}
        }
    }

    fn text(&mut self, raw: &str) {
        let text = raw.trim();
        if text.is_empty() {
            return;
        }
        let tag = self.stack.last().copied();
        let parent = if self.stack.len() >= 2 {
            Some(self.stack[self.stack.len() - 2])
        } else {
            None
        };

        match (tag, parent) {
            (Some(Tag::Family), Some(Tag::Name)) => {
                if let Some(p) = self.player.as_mut() {
                    p.name.family = text.to_string();
                }
            }
            (Some(Tag::Given), Some(Tag::Name)) => {
                if let Some(p) = self.player.as_mut() {
                    p.name.given.push(text.to_string());
                }
            }
            (Some(Tag::Suffix), Some(Tag::Name)) => {
                if let Some(p) = self.player.as_mut() {
                    p.name.suffix = Some(text.to_string());
                }
            }
            (Some(Tag::Family), Some(Tag::Alias)) => {
                if let Some(a) = self.cur_alias.as_mut() {
                    a.family = text.to_string();
                }
            }
            (Some(Tag::Given), Some(Tag::Alias)) => {
                if let Some(a) = self.cur_alias.as_mut() {
                    a.given.push(text.to_string());
                }
            }
            (Some(Tag::Suffix), Some(Tag::Alias)) => {
                if let Some(a) = self.cur_alias.as_mut() {
                    a.suffix = Some(text.to_string());
                }
            }
            (Some(Tag::Federation), Some(Tag::Player)) => {
                if let Some(p) = self.player.as_mut() {
                    p.federation = Some(text.to_string());
                }
            }
            // Repeatable — push, don't overwrite. An earlier version of
            // this parser used `Option<String>` here, which silently kept
            // only the *last* `<title>` for players with more than one
            // (e.g. `IM+WGM`), before that field was even being compared.
            (Some(Tag::Title), Some(Tag::Player)) => {
                if let Some(p) = self.player.as_mut() {
                    p.titles.push(text.to_string());
                }
            }
            (Some(Tag::Sex), Some(Tag::Player)) => {
                if let Some(p) = self.player.as_mut() {
                    p.female = text == "F";
                }
            }
            (Some(Tag::FideId), Some(Tag::Ids)) => {
                if let Some(p) = self.player.as_mut() {
                    p.fide_id = text.parse().ok();
                }
            }
            (Some(Tag::Current), Some(Tag::RatingTrack)) => {
                if let Some(p) = self.player.as_mut() {
                    p.current_rating = text.parse().ok();
                }
            }
            (Some(Tag::Value), Some(Tag::Peak)) => {
                if let Some(p) = self.player.as_mut() {
                    p.peak_value = text.parse().ok();
                }
            }
            (Some(Tag::Month), Some(Tag::Year)) => {
                if let (Some(year), Some(num), Some(p)) =
                    (self.cur_history_year, self.cur_history_month, self.player.as_mut())
                {
                    if (1..=12).contains(&num) {
                        let entry = p.rating_history.entry(year).or_insert([None; 12]);
                        entry[(num - 1) as usize] = if text == "?" { None } else { text.parse().ok() };
                    }
                }
            }
            _ => {}
        }
    }
}

fn parse_one_file(path: &Path, out: &mut ParsePlayersResult) -> std::io::Result<()> {
    let file = File::open(path)?;
    let buffered = BufReader::with_capacity(1 << 20, file);
    let mut xml = Reader::from_reader(buffered);
    xml.config_mut().trim_text(true);
    let mut buf = Vec::with_capacity(1 << 16);

    let mut p = FileParser::new(out);

    loop {
        match xml.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Start(e)) => {
                let tag = tag_of(e.local_name().as_ref());
                p.enter(tag, &e);
            }
            Ok(Event::Empty(e)) => {
                let tag = tag_of(e.local_name().as_ref());
                p.enter(tag, &e);
                p.exit();
            }
            Ok(Event::End(_)) => {
                p.exit();
            }
            Ok(Event::Text(e)) => {
                if let Ok(text) = e.unescape() {
                    p.text(&text);
                }
            }
            Ok(_) => {}
            Err(err) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!("{}: {err}", path.display()),
                ));
            }
        }
        buf.clear();
    }

    Ok(())
}

pub fn parse_players_dir(dir: &Path) -> std::io::Result<ParsePlayersResult> {
    let mut files: Vec<_> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("xml"))
        .collect();
    files.sort();

    let mut out = ParsePlayersResult::default();
    for file in &files {
        parse_one_file(file, &mut out)?;
    }
    out.shards = files.len();
    Ok(out)
}
