//! Streaming counters over the CTML v2 XML registries (`assets/registries/`).
//!
//! These read event-by-event with `quick_xml`'s pull parser instead of
//! building a DOM, so a 500MB shard costs a fixed small buffer, not its own
//! size in RAM. That matters here: `assets/registries/players/` alone is
//! ~4.6GB across 27 shards.

use quick_xml::events::Event;
use quick_xml::Reader;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

#[derive(Default, Debug, Clone, Copy)]
pub struct RegistryCounts {
    pub records: u64,
    pub sub_records: u64,
}

/// Count occurrences of `record_tag` (and optionally `sub_tag`, nested
/// inside it) across one XML file, by local name (namespace prefix
/// ignored — every file here uses `ctml:`, but matching on local name
/// only means a prefix change doesn't silently zero out the count).
fn count_elements(
    path: &Path,
    record_tag: &[u8],
    sub_tag: Option<&[u8]>,
) -> std::io::Result<RegistryCounts> {
    let file = File::open(path)?;
    let buffered = BufReader::with_capacity(1 << 20, file);
    let mut xml = Reader::from_reader(buffered);
    xml.config_mut().trim_text(true);

    let mut buf = Vec::with_capacity(1 << 16);
    let mut counts = RegistryCounts::default();

    loop {
        match xml.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Start(e)) | Ok(Event::Empty(e)) => {
                let name = e.local_name();
                if name.as_ref() == record_tag {
                    counts.records += 1;
                } else if sub_tag == Some(name.as_ref()) {
                    counts.sub_records += 1;
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

    Ok(counts)
}

/// `assets/registries/events.xml`: counts `ctml:eventSeries`.
pub fn scan_events(path: &Path) -> std::io::Result<RegistryCounts> {
    count_elements(path, b"eventSeries", None)
}

fn xml_files_sorted(dir: &Path) -> std::io::Result<Vec<std::path::PathBuf>> {
    let mut files: Vec<_> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("xml"))
        .collect();
    files.sort();
    Ok(files)
}

/// `assets/registries/players/players-*.xml`: sums `ctml:player` and
/// nested `ctml:alias` across every shard.
pub fn scan_players_dir(dir: &Path) -> std::io::Result<(RegistryCounts, usize)> {
    let files = xml_files_sorted(dir)?;
    let mut total = RegistryCounts::default();
    for file in &files {
        let c = count_elements(file, b"player", Some(b"alias"))?;
        total.records += c.records;
        total.sub_records += c.sub_records;
    }
    Ok((total, files.len()))
}

/// `assets/registries/places/places-*.xml`: sums `ctml:place` across every
/// shard.
pub fn scan_places_dir(dir: &Path) -> std::io::Result<(RegistryCounts, usize)> {
    let files = xml_files_sorted(dir)?;
    let mut total = RegistryCounts::default();
    for file in &files {
        let c = count_elements(file, b"place", None)?;
        total.records += c.records;
    }
    Ok((total, files.len()))
}
