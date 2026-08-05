//! Crosstable → `ctml:tournament` conversion. A direct Rust port of
//! `D:\dev\proj\ctml\readers\ctml_source_common.py::crosstable_to_ctml` —
//! the actual function the Python pipeline uses for this, ported term for
//! term (including its exact XML element order, since `xs:sequence` in
//! `xsd/ctml-core.xsd` requires it) rather than redesigned, so this stays
//! swappable with the Python output.
//!
//! **Scope, stated plainly:** this converts each raw crosstable entry in
//! `assets/crosstables.json` independently. It does *not* replicate
//! `scripts/curate_source_tournaments.py`'s clustering step, which merges
//! multiple scrapers' captures of the same real-world tournament (TWIC +
//! OlimpBase + chess-results, etc.) before conversion — that step reads
//! several raw source trees this repo doesn't carry, only
//! `crosstables.json` itself. One `ctml:tournament` file per JSON entry,
//! not one per real tournament; duplicate-event dedup is a separate task.

use crate::crosstable::CrosstableEntry;
use crate::names::split_name;
use crate::xmlutil::{esc, format_g, normalize_space, sha1_hex16, slug, xml_id};

const TITLE_VALUES: &[&str] = &["GM", "IM", "FM", "CM", "WGM", "WIM", "WFM", "WCM", "NM"];
const EVENT_FORMATS: &[&str] =
    &["round-robin", "swiss", "match", "team", "knockout", "scheveningen", "other", "unknown"];
const CADENCES: &[&str] = &["classical", "rapid", "blitz", "bullet", "correspondence", "mixed", "unknown"];
const RATING_SYSTEMS: &[&str] =
    &["fide", "uscf", "ecf", "national", "online", "edo", "chessmetrics", "combined", "none", "unknown"];

// ---------------------------------------------------------------------
// PartialDate — xsd/ctml-dates.xsd's PartialDateType: a choice of
// year/month/day precision wrapper elements, each carrying only the
// attributes valid at that precision (no v1-style flat @precision
// attribute — XSD 1.0 can't express that as conditionally-required).
// ---------------------------------------------------------------------

pub struct PartialDate {
    pub y: i32,
    pub m: Option<u32>,
    pub d: Option<u32>,
}

impl PartialDate {
    fn precision(&self) -> &'static str {
        if self.d.is_some() {
            "day"
        } else if self.m.is_some() {
            "month"
        } else {
            "year"
        }
    }

    fn is_calendar_day(&self) -> bool {
        let (Some(m), Some(d)) = (self.m, self.d) else { return false };
        let leap = self.y % 4 == 0 && (self.y % 100 != 0 || self.y % 400 == 0);
        let days = [0, 31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
        (1..=12).contains(&m) && d >= 1 && d <= days[m as usize]
    }

    fn inner_attrs(&self) -> String {
        let mut attrs = format!(r#"y="{}""#, self.y);
        if let Some(m) = self.m {
            attrs.push_str(&format!(r#" m="{m}""#));
        }
        if let Some(d) = self.d {
            attrs.push_str(&format!(r#" d="{d}""#));
        }
        if self.is_calendar_day() {
            attrs.push_str(&format!(r#" iso="{:04}-{:02}-{:02}""#, self.y, self.m.unwrap(), self.d.unwrap()));
        }
        attrs
    }

    /// `<ctml:{tag}><ctml:{precision} .../></ctml:{tag}>`
    pub fn element(&self, tag: &str) -> String {
        format!("<ctml:{tag}><ctml:{} {}/></ctml:{tag}>", self.precision(), self.inner_attrs())
    }

    pub fn compact(&self) -> String {
        format!("{:04}{:02}{:02}", self.y, self.m.unwrap_or(0), self.d.unwrap_or(0))
    }
}

fn all_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn parse_yyyymmdd(text: &str) -> Option<PartialDate> {
    if text.len() != 8 || !all_digits(text) {
        return None;
    }
    let y: i32 = text[0..4].parse().ok()?;
    let m: u32 = text[4..6].parse().ok()?;
    let d: u32 = text[6..8].parse().ok()?;
    if m == 0 {
        Some(PartialDate { y, m: None, d: None })
    } else if d == 0 {
        Some(PartialDate { y, m: Some(m), d: None })
    } else {
        Some(PartialDate { y, m: Some(m), d: Some(d) })
    }
}

/// `"YYYY"`, `"YYYY-MM"`, or `"YYYY-MM-DD"`; falls back to
/// [`parse_yyyymmdd`] (bare `YYYYMMDD`, no separators) for anything else,
/// matching `parse_iso_date`'s own fallback.
fn parse_iso_date(text: &str) -> Option<PartialDate> {
    let t = normalize_space(text);
    let b = t.as_bytes();
    if t.len() == 4 && all_digits(&t) {
        return Some(PartialDate { y: t.parse().ok()?, m: None, d: None });
    }
    if t.len() == 7 && b[4] == b'-' && all_digits(&t[..4]) && all_digits(&t[5..7]) {
        return Some(PartialDate { y: t[..4].parse().ok()?, m: Some(t[5..7].parse().ok()?), d: None });
    }
    if t.len() == 10 && b[4] == b'-' && b[7] == b'-' && all_digits(&t[..4]) && all_digits(&t[5..7]) && all_digits(&t[8..10])
    {
        return Some(PartialDate {
            y: t[..4].parse().ok()?,
            m: Some(t[5..7].parse().ok()?),
            d: Some(t[8..10].parse().ok()?),
        });
    }
    parse_yyyymmdd(&t)
}

// ---------------------------------------------------------------------
// Field normalization — each mirrors one `ctml_source_common.py` helper.
// ---------------------------------------------------------------------

fn normalize_fed(value: &str) -> String {
    let text = normalize_space(value).to_uppercase();
    if text.len() == 3 && text.bytes().all(|b| b.is_ascii_uppercase()) {
        text
    } else {
        String::new()
    }
}

/// Returns `(recognized_title, raw_title)` — exactly one is non-empty
/// when the input is non-empty. Single-letter shorthand (`G`/`M`/`F`/`C`/
/// `N`) is expanded first, matching `normalize_title`.
fn normalize_title(value: &str) -> (String, String) {
    let mut raw = normalize_space(value).to_uppercase();
    raw = match raw.as_str() {
        "G" => "GM".to_string(),
        "M" => "IM".to_string(),
        "F" => "FM".to_string(),
        "C" => "CM".to_string(),
        "N" => "NM".to_string(),
        _ => raw,
    };
    if TITLE_VALUES.contains(&raw.as_str()) {
        (raw, String::new())
    } else {
        (String::new(), raw)
    }
}

fn map_event_format(value: &str) -> String {
    let text = normalize_space(value).to_lowercase();
    if EVENT_FORMATS.contains(&text.as_str()) {
        return text;
    }
    if text.contains("schweizer") || text.contains("swiss") {
        "swiss".to_string()
    } else if text.contains("rundenturnier") || text.contains("round robin") || text.contains("round-robin") {
        "round-robin".to_string()
    } else if text.contains("match") {
        "match".to_string()
    } else if text.contains("team") {
        "team".to_string()
    } else {
        "unknown".to_string()
    }
}

/// Word-boundary-ish keyword search over lowercased `texts`, for the
/// fallback path when `cadence` is missing/invalid on the source entry.
/// Not a full port of the Python's Unicode regex (`\b(blitz|блиц)\b`
/// etc.) — a simple tokenized-word match instead, which is enough for a
/// low-stakes fallback that our real data rarely even reaches (its own
/// `cadence` field is valid in the overwhelming majority of entries).
fn infer_cadence(texts: &[&str]) -> Option<String> {
    let haystack = texts.join(" ").to_lowercase();
    let words: Vec<&str> = haystack.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).collect();
    let has = |targets: &[&str]| words.iter().any(|w| targets.contains(w));
    if has(&["blitz", "блиц"]) {
        Some("blitz".to_string())
    } else if has(&["rapid", "рапид", "rapide"]) {
        Some("rapid".to_string())
    } else if has(&["bullet"]) {
        Some("bullet".to_string())
    } else if has(&["classical", "классика", "standard"]) {
        Some("classical".to_string())
    } else {
        None
    }
}

fn synth_player_ref(name: &str, fed: &str) -> String {
    let base = format!("{}|{}", name.trim().to_lowercase(), fed.trim().to_uppercase());
    format!("player:syn:{}", sha1_hex16(&base))
}

fn place_raw_ref(site: &str) -> String {
    format!("place:raw:{}", sha1_hex16(&site.trim().to_lowercase()))
}

fn is_valid_fide_id(s: &str) -> bool {
    (4..=12).contains(&s.len()) && all_digits(s)
}

/// `(ref, method)` — `method == "unresolved"` means a synthetic ref with
/// no registry identity, matching `<ctml:resolution method="unresolved">`.
fn player_ref_for(explicit_ref: Option<&str>, fide_id: Option<&str>, name: &str, fed: &str) -> (String, &'static str) {
    if let Some(r) = explicit_ref.map(str::trim).filter(|r| !r.is_empty()) {
        return if r.starts_with("player:syn:") { (r.to_string(), "unresolved") } else { (r.to_string(), "") };
    }
    if let Some(id) = fide_id.map(str::trim).filter(|id| is_valid_fide_id(id)) {
        return (format!("player:fide:{id}"), "fide-id");
    }
    (synth_player_ref(name, fed), "unresolved")
}

fn person_name_xml(raw: &str, indent: &str) -> String {
    let raw = {
        let n = normalize_space(raw);
        if n.is_empty() { "Unknown".to_string() } else { n }
    };
    let mut lines = vec![format!(r#"{indent}<ctml:name display="{}">"#, esc(&raw))];
    let (family, given, suffix) = split_name(&raw);
    if family.is_empty() && given.is_empty() {
        lines.push(format!("{indent}  <ctml:unstructured>Unknown</ctml:unstructured>"));
    } else {
        lines.push(format!("{indent}  <ctml:family>{}</ctml:family>", esc(&family)));
        for g in &given {
            lines.push(format!("{indent}  <ctml:given>{}</ctml:given>", esc(g)));
        }
        if let Some(s) = &suffix {
            lines.push(format!("{indent}  <ctml:suffix>{}</ctml:suffix>", esc(s)));
        }
    }
    lines.push(format!("{indent}</ctml:name>"));
    lines.join("\n")
}

/// The one real difference from `person_name_xml` in
/// `ctml_source_common.py`: that version's no-comma branch treats a
/// *single* token as family-only (no `<unstructured>`), same as
/// `names::split_name` already does — `split_name` returning empty
/// family AND empty given only happens for a genuinely blank name, which
/// is exactly the case this function's `<unstructured>` branch is for.

pub fn crosstable_to_ctml(t: &CrosstableEntry) -> Option<String> {
    let event = normalize_space(&t.event);
    let players: Vec<_> = t.players.iter().filter(|p| !normalize_space(&p.name).is_empty()).collect();
    if event.is_empty() || players.is_empty() {
        return None;
    }

    let start = t.start.as_deref().and_then(parse_iso_date)?;
    let end = t.end.as_deref().and_then(parse_iso_date).unwrap_or_else(|| PartialDate {
        y: start.y,
        m: start.m,
        d: start.d,
    });

    let source = {
        let s = normalize_space(t.source.as_deref().unwrap_or(""));
        if s.is_empty() { "source".to_string() } else { s }
    };
    let raw_ref = t.ref_.clone().filter(|r| !r.trim().is_empty());
    let ref_for_id = raw_ref.clone().unwrap_or_else(|| {
        sha1_hex16(&serde_json::to_string(&serde_json::json!({
            "event": t.event, "start": t.start, "source": t.source,
        })).unwrap_or_default())
    });
    let tid = xml_id(&[
        "t",
        &source.replace('-', "_"),
        &slug(&ref_for_id, "unknown"),
        &start.compact(),
    ]);

    let fmt = map_event_format(&t.format);
    let cadence = {
        let c = normalize_space(t.cadence.as_deref().unwrap_or(""));
        if CADENCES.contains(&c.as_str()) {
            c
        } else {
            infer_cadence(&[&event, t.notes.as_deref().unwrap_or("")]).unwrap_or_default()
        }
    };
    let rating_system = {
        let r = normalize_space(t.rating_system.as_deref().unwrap_or(""));
        let r = if r.is_empty() { "unknown".to_string() } else { r };
        if RATING_SYSTEMS.contains(&r.as_str()) { r } else { "unknown".to_string() }
    };

    let mut lines = vec![
        r#"<?xml version="1.0" encoding="UTF-8"?>"#.to_string(),
        format!(r#"<ctml:tournament xmlns:ctml="urn:ctml:2.0" ctmlVersion="2.0" id="{}">"#, esc(&tid)),
        "  <ctml:header>".to_string(),
        format!("    <ctml:name>{}</ctml:name>", esc(&event)),
    ];

    if let Some(event_ref) = t.event_ref.as_deref().map(normalize_space).filter(|s| !s.is_empty()) {
        lines.push(format!(r#"    <ctml:eventRef ref="{}">"#, esc(&event_ref)));
        lines.push(format!("      <ctml:name>{}</ctml:name>", esc(&event)));
        lines.push("    </ctml:eventRef>".to_string());
    }
    if fmt != "unknown" {
        lines.push(format!("    <ctml:eventType>{fmt}</ctml:eventType>"));
    }
    if !cadence.is_empty() {
        lines.push(format!("    <ctml:cadence>{cadence}</ctml:cadence>"));
    }
    lines.push("    <ctml:dates>".to_string());
    lines.push(format!("      {}", start.element("start")));
    lines.push(format!("      {}", end.element("end")));
    lines.push("    </ctml:dates>".to_string());

    let place = t.place.as_deref().map(normalize_space).filter(|s| !s.is_empty());
    let country = t.country.as_deref().map(normalize_fed).filter(|s| !s.is_empty());
    if let Some(place) = &place {
        let kind = if country.as_deref() == Some(place.as_str()) { "country" } else { "unknown" };
        lines.push(format!(r#"    <ctml:placeRef ref="{}" kind="{kind}">"#, esc(&place_raw_ref(place))));
        lines.push(format!("      <ctml:name>{}</ctml:name>", esc(place)));
        if let Some(c) = &country {
            lines.push(format!("      <ctml:country>{c}</ctml:country>"));
        }
        lines.push("    </ctml:placeRef>".to_string());
    }
    lines.push("  </ctml:header>".to_string());

    lines.push("  <ctml:participants>".to_string());
    for (idx, player) in players.iter().enumerate() {
        let pid = format!("p{:04}", idx + 1);
        let name = {
            let n = normalize_space(&player.name);
            if n.is_empty() { "Unknown".to_string() } else { n }
        };
        let fed = normalize_fed(player.fed.as_deref().unwrap_or(""));
        let (title, raw_title) = normalize_title(player.title.as_deref().unwrap_or(""));
        let (player_ref, method) =
            player_ref_for(player.ref_.as_deref(), player.fide_id.as_deref(), &name, &fed);

        lines.push(format!(r#"    <ctml:participant id="{pid}">"#));
        let source_attr = t
            .url
            .as_deref()
            .map(normalize_space)
            .filter(|s| !s.is_empty())
            .map(|u| format!(r#" source="{}""#, esc(&u)))
            .unwrap_or_default();
        lines.push(format!(r#"      <ctml:playerRef ref="{}"{source_attr}>"#, esc(&player_ref)));
        lines.push(person_name_xml(&name, "        "));
        if !fed.is_empty() {
            lines.push(format!("        <ctml:federation>{fed}</ctml:federation>"));
        }
        if !title.is_empty() {
            lines.push(format!("        <ctml:title>{title}</ctml:title>"));
        }
        let fide_id = player.fide_id.as_deref().map(str::trim).filter(|id| is_valid_fide_id(id));
        let internal_id = player
            .source_id
            .as_ref()
            .and_then(|v| v.to_int_like())
            .or_else(|| player.seed.as_ref().and_then(|v| v.to_int_like()));
        if fide_id.is_some() || internal_id.is_some() {
            lines.push("        <ctml:ids>".to_string());
            if let Some(id) = fide_id {
                lines.push(format!("          <ctml:fideId>{id}</ctml:fideId>"));
            }
            if let Some(id) = internal_id {
                lines.push(format!("          <ctml:internalId>{}:{id}</ctml:internalId>", esc(&source)));
            }
            lines.push("        </ctml:ids>".to_string());
        }
        if method == "unresolved" {
            let resolver = t.reader.as_deref().map(normalize_space).filter(|s| !s.is_empty()).unwrap_or_else(|| source.clone());
            lines.push(format!(r#"        <ctml:resolution method="unresolved" resolver="{}"/>"#, esc(&resolver)));
        }
        lines.push("      </ctml:playerRef>".to_string());

        if let Some(rating) = player.rating {
            lines.push(format!(r#"      <ctml:ratingSnapshot system="{rating_system}" scope="standard">"#));
            lines.push(format!("        <ctml:value>{rating}</ctml:value>"));
            lines.push(format!("        {}", start.element("asOf")));
            lines.push("      </ctml:ratingSnapshot>".to_string());
        }
        if let Some(seed) = player.seed.as_ref().and_then(|v| v.to_int_like()) {
            lines.push(format!("      <ctml:seed>{seed}</ctml:seed>"));
        }
        if let Some(score) = player.score {
            lines.push(format!("      <ctml:score>{}</ctml:score>", format_g(score)));
        }

        let mut notes = Vec::new();
        if let Some(rank) = player.rank {
            notes.push(format!("rank={rank}"));
        }
        if !raw_title.is_empty() {
            notes.push(format!("raw_title={raw_title}"));
        }
        if let Some(v) = player.club.as_deref().map(normalize_space).filter(|s| !s.is_empty()) {
            notes.push(format!("club={v}"));
        }
        if let Some(v) = player.sex.as_deref().map(normalize_space).filter(|s| !s.is_empty()) {
            notes.push(format!("sex={v}"));
        }
        if !notes.is_empty() {
            lines.push(format!("      <ctml:notes>{}</ctml:notes>", esc(&notes.join("; "))));
        }
        lines.push("    </ctml:participant>".to_string());
    }
    lines.push("  </ctml:participants>".to_string());

    let mut doc_notes = Vec::new();
    if let Some(n) = t.notes.as_deref().map(normalize_space).filter(|s| !s.is_empty()) {
        doc_notes.push(n);
    }
    if t.classification.as_deref() == Some("team") {
        doc_notes.push(
            "Source table was classified as team standings and should be reviewed before publication.".to_string(),
        );
    }
    if !doc_notes.is_empty() {
        lines.push(format!("  <ctml:notes>{}</ctml:notes>", esc(&doc_notes.join(" "))));
    }

    let source_path = t.source_path.as_deref().map(normalize_space).filter(|s| !s.is_empty());
    let source_url = t.url.as_deref().map(normalize_space).filter(|s| !s.is_empty());
    lines.push(format!(r#"  <ctml:source kind="{}">"#, esc(&source)));
    if let Some(u) = &source_url {
        lines.push(format!("    <ctml:uri>{}</ctml:uri>", esc(u)));
    }
    let mut source_note = format!("ref={ref_for_id}");
    if let Some(p) = &source_path {
        source_note.push_str(&format!("; source_path={p}"));
    }
    lines.push(format!("    <ctml:note>{}</ctml:note>", esc(&source_note)));
    lines.push("  </ctml:source>".to_string());
    lines.push("</ctml:tournament>".to_string());
    lines.push(String::new());

    Some(lines.join("\n"))
}

/// Output filename, matching `write_ctml_files`'s `{ref}-{event}` slug
/// convention (minus its `seen`-based disambiguation counter, applied by
/// the caller since it needs to track uniqueness across the whole batch).
pub fn base_filename(t: &CrosstableEntry, index: usize) -> String {
    let ref_ = t.ref_.clone().filter(|r| !r.trim().is_empty()).unwrap_or_else(|| (index + 1).to_string());
    let event = {
        let e = normalize_space(&t.event);
        if e.is_empty() { "event".to_string() } else { e }
    };
    format!("{}-{}", slug(&ref_, "unknown"), slug(&event, "event"))
}
