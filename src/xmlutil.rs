//! Small string/XML helpers ported from
//! `D:\dev\proj\ctml\readers\ctml_source_common.py`, used by
//! [`crate::tournament`]. Kept together because none of them are
//! tournament-specific — `esc`/`slug`/`sha1_hex16` are generic enough that
//! any future writer (PGN import, dedup) would reach for the same ones.

use sha1::{Digest, Sha1};

/// `xml.sax.saxutils.escape(s, {'"': "&quot;"})` — `&` first, so it
/// doesn't double-escape the entities this introduces.
pub fn esc(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(ch),
        }
    }
    out
}

/// `normalize_space`: collapses runs of whitespace (including NBSP) to a
/// single space and trims. The Python original also runs `html.unescape`
/// first; skipped here because `assets/crosstables.json` was checked
/// directly and contains zero HTML-entity-looking substrings — if a
/// future source does, this needs a real unescape pass, not a guess.
pub fn normalize_space(value: &str) -> String {
    value.replace('\u{a0}', " ").split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `slug()`: lowercase, Unicode-alphanumeric runs joined by single
/// dashes; whitespace/`_`/`-` become dash boundaries; anything else is
/// dropped outright (not replaced — matches the Python, which only ever
/// appends on the alnum branch).
pub fn slug(value: &str, fallback: &str) -> String {
    let text = normalize_space(value).to_lowercase();
    let mut out = String::new();
    let mut pending_dash = false;
    for ch in text.chars() {
        if ch.is_alphanumeric() {
            if pending_dash && !out.is_empty() {
                out.push('-');
            }
            pending_dash = false;
            out.push(ch);
        } else if ch.is_whitespace() || ch == '_' || ch == '-' {
            pending_dash = true;
        }
    }
    if out.is_empty() {
        fallback.to_string()
    } else {
        out
    }
}

/// First 16 hex chars (8 bytes) of the SHA-1 digest of `data` — matches
/// Python's `hashlib.sha1(data.encode()).hexdigest()[:16]` exactly (same
/// algorithm, same truncation), used throughout the pipeline as a stable
/// short id (`player:syn:...`, `place:raw:...`, tournament ids).
pub fn sha1_hex16(data: &str) -> String {
    let digest = Sha1::digest(data.as_bytes());
    let mut out = String::with_capacity(16);
    for byte in digest.iter().take(8) {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// `xml_id(*parts)`: parts joined by `_`, anything outside
/// `[A-Za-z0-9_.-]` collapsed to `_`, leading/trailing `_.-` trimmed;
/// prefixed `t_` (with a content hash if nothing usable survived) if the
/// result doesn't start with a letter or underscore — `xs:ID` requires a
/// valid `NCName`, which can't start with a digit. Truncated to 180
/// chars.
pub fn xml_id(parts: &[&str]) -> String {
    let raw = parts.iter().filter(|p| !p.is_empty()).copied().collect::<Vec<_>>().join("_");
    let mut cleaned = String::with_capacity(raw.len());
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '.' || ch == '-' {
            cleaned.push(ch);
        } else {
            cleaned.push('_');
        }
    }
    let cleaned = cleaned.trim_matches(|c| c == '_' || c == '.' || c == '-').to_string();

    let needs_prefix = cleaned.is_empty()
        || !cleaned.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_');
    let result = if needs_prefix {
        if cleaned.is_empty() {
            format!("t_{}", sha1_hex16(&raw))
        } else {
            format!("t_{cleaned}")
        }
    } else {
        cleaned
    };

    result.chars().take(180).collect()
}

/// Formats a float the way Python's `f"{x:g}"` would for the small
/// values this pipeline actually sees (scores, ratios): no trailing
/// zeros, no trailing `.0`. Not a general `%g` implementation — doesn't
/// switch to scientific notation, which chess scores never need.
pub fn format_g(value: f64) -> String {
    if value == value.trunc() {
        format!("{value:.0}")
    } else {
        let s = format!("{value}");
        s
    }
}
