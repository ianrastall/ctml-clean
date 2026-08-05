//! Shared name-splitting logic: `person_name_xml`'s family/given/suffix
//! rule appears twice in the actual Python pipeline (once in
//! `scripts/ssp_to_ctml_players.py` for the player registry, once in
//! `D:\dev\proj\ctml\readers\ctml_source_common.py` for crosstable
//! participants) — byte-for-byte the same algorithm both places, so it
//! gets one Rust implementation here instead of two.

/// Generation-suffix tokens `person_name_xml` pulls out of the *comma*
/// form only — matched case-insensitively against the last
/// whitespace-separated token after the comma. Includes bare Roman
/// numerals ("i".."vi"), which is why a name like `"Varshini, V"` loses
/// its apparent given name: `V` reads as suffix "5th", not an initial.
pub const SUFFIXES: &[&str] = &["jr", "jr.", "sr", "sr.", "i", "ii", "iii", "iv", "v", "vi", "2nd", "3rd"];

/// Splits one display name into `(family, given[], suffix)`, replicating
/// `person_name_xml` exactly (verified against the real player registry:
/// a full 529,403-player diff came back with 0 given/suffix mismatches
/// once this matched the script — see `HANDOFF.md`):
///
/// - Comma present (`"Family, Given Given [Suffix]"`): family is
///   everything before the comma. The remainder is whitespace-split; if
///   its *last* token case-insensitively matches [`SUFFIXES`], that token
///   is pulled out as the suffix (original casing kept) rather than
///   treated as a given name. Everything else becomes one given-name
///   entry per word, in order.
/// - No comma (`"Given Given Family"`, or a bare mononym): the *last*
///   word is the family name; every word before it becomes its own
///   given-name entry, in order. No suffix extraction happens in this
///   branch at all — that's a real asymmetry in the generator, not an
///   oversight here. A single-word name yields no given names.
pub fn split_name(display: &str) -> (String, Vec<String>, Option<String>) {
    if let Some(idx) = display.find(',') {
        let family = display[..idx].trim().to_string();
        let mut tokens: Vec<&str> = display[idx + 1..].split_whitespace().collect();
        let mut suffix = None;
        if let Some(&last) = tokens.last() {
            if SUFFIXES.contains(&last.to_lowercase().as_str()) {
                suffix = Some(last.to_string());
                tokens.pop();
            }
        }
        (family, tokens.into_iter().map(|s| s.to_string()).collect(), suffix)
    } else {
        let words: Vec<&str> = display.split_whitespace().collect();
        match words.split_last() {
            Some((&last, rest)) => (
                last.to_string(),
                rest.iter().map(|s| s.to_string()).collect(),
                None,
            ),
            None => (String::new(), Vec::new(), None),
        }
    }
}
