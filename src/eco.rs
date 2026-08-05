//! `assets/all.tsv`: lichess-style ECO opening table (code, name, PGN, UCI,
//! EPD). Tab-separated, header row, no quoting/escaping in the data — a
//! manual split is correct here and avoids pulling in a CSV crate for one
//! trivial 788KB file.

use std::fs;
use std::path::Path;

pub struct EcoRow {
    pub code: String,
    pub name: String,
    pub pgn: String,
    pub uci: String,
    pub epd: String,
}

pub fn load(path: &Path) -> std::io::Result<Vec<EcoRow>> {
    let text = fs::read_to_string(path)?;
    let mut rows = Vec::new();

    for (i, line) in text.lines().enumerate() {
        if i == 0 {
            continue; // header: eco  name  pgn  uci  epd
        }
        if line.is_empty() {
            continue;
        }
        let mut fields = line.splitn(5, '\t');
        let (Some(code), Some(name), Some(pgn), Some(uci), Some(epd)) = (
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
            fields.next(),
        ) else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("{}:{}: expected 5 tab-separated fields", path.display(), i + 1),
            ));
        };
        rows.push(EcoRow {
            code: code.to_string(),
            name: name.to_string(),
            pgn: pgn.to_string(),
            uci: uci.to_string(),
            epd: epd.to_string(),
        });
    }

    Ok(rows)
}
