//! ctml-clean: the Rust half of the CTML project. This binary is meant to
//! become the fast, industrial-strength counterpart to the existing Python
//! pipeline (`D:\dev\proj\ctml`) — same job (crosstable ingest, player/site/
//! event registries, PGN import, Zobrist fingerprinting, dedup), C-like
//! speed. It is not there yet.
//!
//! `stats` is the first real command: it touches every source of truth this
//! repo carries — the SSP master file, the two XML registries, the ECO
//! table, and the scraped crosstables — end to end, at full size, with
//! timing. It reads and counts; it does not yet convert or write anything.
//! That ordering is deliberate: get honest numbers for what's actually on
//! disk before writing code that transforms it.

mod chess;
mod crosstable;
mod diff;
mod eco;
mod fingerprint;
mod names;
mod polyglot_array;
mod registry;
mod ssp;
mod tournament;
mod xmlplayers;
mod xmlutil;

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let command = args.next();

    match command.as_deref() {
        Some("stats") | None => {
            let assets_dir = args
                .next()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("assets"));
            match run_stats(&assets_dir) {
                Ok(()) => ExitCode::SUCCESS,
                Err(err) => {
                    eprintln!("error: {err}");
                    ExitCode::FAILURE
                }
            }
        }
        Some("diff-players") => {
            let assets_dir = args
                .next()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("assets"));
            match run_diff_players(&assets_dir) {
                Ok(()) => ExitCode::SUCCESS,
                Err(err) => {
                    eprintln!("error: {err}");
                    ExitCode::FAILURE
                }
            }
        }
        Some("ingest-crosstables") => {
            let assets_dir = args
                .next()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("assets"));
            let out_dir = args
                .next()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("out/tournaments"));
            match run_ingest_crosstables(&assets_dir, &out_dir) {
                Ok(()) => ExitCode::SUCCESS,
                Err(err) => {
                    eprintln!("error: {err}");
                    ExitCode::FAILURE
                }
            }
        }
        Some("fingerprint-selftest") => {
            let spec_path = args
                .next()
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("spec/fingerprint.md"));
            match run_fingerprint_selftest(&spec_path) {
                Ok(true) => ExitCode::SUCCESS,
                Ok(false) => ExitCode::FAILURE,
                Err(err) => {
                    eprintln!("error: {err}");
                    ExitCode::FAILURE
                }
            }
        }
        Some(other) => {
            eprintln!(
                "ctml-clean: unknown command '{other}'\nusage: ctml-clean <stats|diff-players|ingest-crosstables|fingerprint-selftest> [args...]"
            );
            ExitCode::FAILURE
        }
    }
}

/// Parses the test-vector table directly out of `spec/fingerprint.md`
/// (rather than retyping 6 hex strings by hand into this file, which is
/// exactly the kind of transcription error the spec's own opening
/// paragraph warns about) and checks every row.
fn run_fingerprint_selftest(spec_path: &Path) -> std::io::Result<bool> {
    let text = std::fs::read_to_string(spec_path)?;
    let mut cases: Vec<(String, Vec<String>, String, String)> = Vec::new();

    for line in text.lines() {
        let line = line.trim();
        if !line.starts_with('|') || !line.contains('`') {
            continue;
        }
        let cells: Vec<&str> = line.trim_matches('|').split('|').map(str::trim).collect();
        if cells.len() != 4 {
            continue;
        }
        let [label, moves_cell, traj_cell, final_cell] = [cells[0], cells[1], cells[2], cells[3]];
        let Some(trajectory) = traj_cell.strip_prefix('`').and_then(|s| s.strip_suffix('`')) else { continue };
        let Some(final_position) = final_cell.strip_prefix('`').and_then(|s| s.strip_suffix('`')) else { continue };
        // Header separator / non-hex rows won't match this once we also
        // require the trajectory cell to look like hex.
        if !trajectory.bytes().all(|b| b.is_ascii_hexdigit()) || trajectory.len() != 64 {
            continue;
        }
        let moves: Vec<String> = if moves_cell.contains("none") {
            Vec::new()
        } else {
            moves_cell.trim_matches('`').split_whitespace().map(str::to_string).collect()
        };
        cases.push((label.to_string(), moves, trajectory.to_string(), final_position.to_string()));
    }

    if cases.is_empty() {
        eprintln!("no test-vector rows found in {} — table format changed?", spec_path.display());
        return Ok(false);
    }

    println!("found {} test vectors in {}\n", cases.len(), spec_path.display());
    let mut all_ok = true;
    for (label, moves, want_traj, want_final) in &cases {
        match fingerprint::compute(moves, None) {
            Some(fp) => {
                let ok = &fp.trajectory == want_traj && &fp.final_position == want_final;
                all_ok &= ok;
                println!(
                    "{} {label}  moves=[{}]",
                    if ok { "PASS" } else { "FAIL" },
                    moves.join(" ")
                );
                if !ok {
                    println!("    trajectory:     got {}", fp.trajectory);
                    println!("                    want {want_traj}");
                    println!("    finalPosition:  got {}", fp.final_position);
                    println!("                    want {want_final}");
                }
            }
            None => {
                all_ok = false;
                println!("FAIL {label}  (move application failed)");
            }
        }
    }

    println!("\n{}", if all_ok { "all vectors match." } else { "MISMATCH — see above." });
    Ok(all_ok)
}

fn run_ingest_crosstables(assets_dir: &Path, out_dir: &Path) -> std::io::Result<()> {
    let crosstables_path = assets_dir.join("crosstables.json");
    println!("parsing {} ...", crosstables_path.display());
    let t = Instant::now();
    let entries = crosstable::load(&crosstables_path)?;
    println!("  {} raw entries   {:>8.2?}\n", entries.len(), t.elapsed());

    std::fs::create_dir_all(out_dir)?;

    let mut written = 0u64;
    let mut skipped_no_start = 0u64;
    let mut skipped_other = 0u64;
    let mut seen: std::collections::HashMap<String, u32> = std::collections::HashMap::new();

    let t = Instant::now();
    for (idx, entry) in entries.iter().enumerate() {
        match tournament::crosstable_to_ctml(entry) {
            Some(xml) => {
                let base = tournament::base_filename(entry, idx);
                let count = seen.entry(base.clone()).or_insert(0);
                *count += 1;
                let suffix = if *count > 1 { format!("-{count}") } else { String::new() };
                let truncated: String = base.chars().take(150).collect();
                let path = out_dir.join(format!("{truncated}{suffix}.xml"));
                std::fs::write(path, xml)?;
                written += 1;
            }
            None => {
                let has_event = !entry.event.trim().is_empty();
                let has_named_player = entry.players.iter().any(|p| !p.name.trim().is_empty());
                if has_event && has_named_player {
                    // Only remaining reason `crosstable_to_ctml` returns
                    // `None`: missing or unparseable `start`.
                    skipped_no_start += 1;
                } else {
                    skipped_other += 1;
                }
            }
        }
    }

    println!(
        "wrote {written} tournament files to {}\nskipped {skipped_no_start} (no usable start date), {skipped_other} (no event name / no named players)   {:>8.2?}",
        out_dir.display(),
        t.elapsed()
    );

    Ok(())
}

fn run_diff_players(assets_dir: &Path) -> std::io::Result<()> {
    let ssp_path = find_ssp(assets_dir)?;
    let players_dir = assets_dir.join("registries/players");

    println!("parsing {} ...", ssp_path.display());
    let t = Instant::now();
    let ssp_result = ssp::parse_players(&ssp_path)?;
    println!(
        "  {} players with a FIDE id, {} without (not compared — no join key), \
         {} malformed name lines, {} comment lines   {:>8.2?}",
        ssp_result.by_fide_id.len(),
        ssp_result.without_fide_id,
        ssp_result.malformed_name_lines,
        ssp_result.comment_lines,
        t.elapsed()
    );

    println!("\nparsing {} ...", players_dir.display());
    let t = Instant::now();
    let xml_result = xmlplayers::parse_players_dir(&players_dir)?;
    println!(
        "  {} players with a FIDE id, {} without, {} shards   {:>8.2?}",
        xml_result.by_fide_id.len(),
        xml_result.without_fide_id,
        xml_result.shards,
        t.elapsed()
    );

    println!("\ncomparing, joined on FIDE id ...\n");
    let t = Instant::now();
    let report = diff::compare(&ssp_result.by_fide_id, &xml_result.by_fide_id);
    diff::print_report(&report);
    println!("\n(comparison took {:>8.2?})", t.elapsed());

    Ok(())
}

fn run_stats(assets_dir: &Path) -> std::io::Result<()> {
    println!("ctml-clean stats — scanning {}\n", assets_dir.display());

    let eco_path = assets_dir.join("all.tsv");
    let t = Instant::now();
    let eco_rows = eco::load(&eco_path)?;
    println!(
        "eco table       {:<40} {:>10} rows                 {:>8.2?}",
        eco_path.display(),
        eco_rows.len(),
        t.elapsed()
    );

    let events_path = assets_dir.join("registries/events.xml");
    let t = Instant::now();
    let events = registry::scan_events(&events_path)?;
    println!(
        "event registry   {:<40} {:>10} eventSeries          {:>8.2?}",
        events_path.display(),
        events.records,
        t.elapsed()
    );

    let players_dir = assets_dir.join("registries/players");
    let t = Instant::now();
    let (players, player_shards) = registry::scan_players_dir(&players_dir)?;
    println!(
        "player registry  {:<40} {:>10} players, {:>7} aliases, {} shards  {:>8.2?}",
        players_dir.display(),
        players.records,
        players.sub_records,
        player_shards,
        t.elapsed()
    );

    let places_dir = assets_dir.join("registries/places");
    let t = Instant::now();
    let (places, place_shards) = registry::scan_places_dir(&places_dir)?;
    println!(
        "place registry   {:<40} {:>10} places, {} shards            {:>8.2?}",
        places_dir.display(),
        places.records,
        place_shards,
        t.elapsed()
    );

    let crosstables_path = assets_dir.join("crosstables.json");
    let t = Instant::now();
    let crosstables = crosstable::load(&crosstables_path)?;
    let total_player_rows: usize = crosstables.iter().map(|c| c.players.len()).sum();
    println!(
        "crosstables      {:<40} {:>10} tournaments, {:>7} player-rows   {:>8.2?}",
        crosstables_path.display(),
        crosstables.len(),
        total_player_rows,
        t.elapsed()
    );

    let ssp_path = find_ssp(assets_dir)?;
    let t = Instant::now();
    let ssp_stats = ssp::scan(&ssp_path)?;
    println!(
        "ssp master       {:<40} {:>10} player records       {:>8.2?}",
        ssp_path.display(),
        ssp_stats.player_records,
        t.elapsed()
    );
    println!(
        "                 {:>10} lines, {:>10} bytes, {} aliases, {} elo lines, {} FIDE ids",
        ssp_stats.lines,
        ssp_stats.bytes,
        ssp_stats.player_aliases,
        ssp_stats.player_elo_lines,
        ssp_stats.player_fide_ids
    );
    println!(
        "                 event-name rules {}, site-name rules {}, round-name rules {}",
        ssp_stats.event_rules, ssp_stats.site_rules, ssp_stats.round_rules
    );

    println!();
    let diff = players.records as i64 - ssp_stats.player_records as i64;
    if diff == 0 {
        println!(
            "check: XML player registry ({}) matches SSP player records exactly.",
            players.records
        );
    } else {
        println!(
            "check: XML player registry has {} players, SSP master has {} player records \
             ({:+} difference) — expected if the registry was generated from a different \
             .ssp snapshot; investigate if this repo's data is meant to be in sync.",
            players.records, ssp_stats.player_records, diff
        );
    }

    Ok(())
}

/// The SSP file's name is date-stamped (`ratings260801.ssp`) and moves as
/// new snapshots land, so find it by extension rather than hardcoding the
/// name.
fn find_ssp(assets_dir: &Path) -> std::io::Result<PathBuf> {
    let mut candidates: Vec<PathBuf> = std::fs::read_dir(assets_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("ssp"))
        .collect();
    candidates.sort();
    candidates.pop().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("no *.ssp file found in {}", assets_dir.display()),
        )
    })
}
