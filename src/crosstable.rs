//! `assets/crosstables.json`: scraped tournament crosstables (event name,
//! format, per-player rank/rating/score rows). This is raw ingest material
//! for `ctml:tournament` — see [`crate::tournament`] for the conversion,
//! which is a Rust port of `D:\dev\proj\ctml\readers\ctml_source_common.py
//! ::crosstable_to_ctml`, the actual function the Python pipeline uses.
//!
//! At 152MB it fits in memory (`serde_json::from_reader` streams the parse
//! but still materializes the full `Vec`), which is fine for now; if this
//! grows past what's comfortable to hold at once, the fix is a top-level
//! streaming array parser (`serde_json::Deserializer::into_iter`), not a
//! different crate.

use serde::Deserialize;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

/// A handful of fields in this scraped JSON (`seed`, seen so far) are
/// sometimes a JSON string and sometimes a JSON number across different
/// entries — real heterogeneity from multiple scrapers, not something to
/// normalize away silently. Checked directly against the full file
/// before assuming this was needed.
#[derive(Deserialize, Debug, Clone)]
#[serde(untagged)]
pub enum FlexScalar {
    Str(String),
    Int(i64),
    Float(f64),
}

impl FlexScalar {
    /// Matches Python's `to_int`: `normalize_space(x)` then
    /// `re.fullmatch(r"\d+", text)` — a plain non-negative integer
    /// string, nothing else (no sign, no decimal point).
    pub fn to_int_like(&self) -> Option<i64> {
        let s = match self {
            FlexScalar::Str(s) => s.trim().to_string(),
            FlexScalar::Int(i) => i.to_string(),
            FlexScalar::Float(_) => return None,
        };
        if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) {
            s.parse().ok()
        } else {
            None
        }
    }
}

#[derive(Deserialize)]
pub struct PlayerRow {
    pub rank: Option<i64>,
    pub name: String,
    pub title: Option<String>,
    pub fed: Option<String>,
    pub rating: Option<i64>,
    pub score: Option<f64>,
    pub fide_id: Option<String>,
    pub seed: Option<FlexScalar>,
    pub source_id: Option<FlexScalar>,
    pub sex: Option<String>,
    pub club: Option<String>,
    #[serde(rename = "ref")]
    pub ref_: Option<String>,
}

#[derive(Deserialize)]
pub struct CrosstableEntry {
    pub event: String,
    #[serde(default)]
    pub header: Option<String>,
    pub format: String,
    pub players: Vec<PlayerRow>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(rename = "ref", default)]
    pub ref_: Option<String>,
    #[serde(default)]
    pub event_ref: Option<String>,
    #[serde(default)]
    pub start: Option<String>,
    #[serde(default)]
    pub end: Option<String>,
    #[serde(default)]
    pub place: Option<String>,
    #[serde(default)]
    pub country: Option<String>,
    #[serde(default)]
    pub cadence: Option<String>,
    #[serde(default)]
    pub classification: Option<String>,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    pub rating_system: Option<String>,
    #[serde(default)]
    pub reader: Option<String>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub source_path: Option<String>,
}

pub fn load(path: &Path) -> std::io::Result<Vec<CrosstableEntry>> {
    let file = File::open(path)?;
    let reader = BufReader::with_capacity(1 << 20, file);
    serde_json::from_reader(reader)
        .map_err(|err| std::io::Error::new(std::io::ErrorKind::InvalidData, err.to_string()))
}
