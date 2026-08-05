//! Field-by-field diff between the typed SSP parse (`crate::ssp`) and the
//! typed XML player-registry parse (`crate::xmlplayers`), joined on FIDE
//! id — the only key both sides carry unambiguously.
//!
//! The point of this module is to turn "the registry was presumably
//! generated from this SSP snapshot" from an assumption into something
//! checked, field by field, across the real 617k-player data — not a
//! sample.

use crate::ssp::SspPlayer;
use crate::xmlplayers::XmlPlayer;
use std::collections::HashMap;

const MAX_EXAMPLES: usize = 8;

pub struct Mismatch {
    pub fide_id: u64,
    pub ssp: String,
    pub xml: String,
}

#[derive(Default)]
struct Field {
    count: u64,
    examples: Vec<Mismatch>,
}

impl Field {
    fn record(&mut self, fide_id: u64, ssp: String, xml: String) {
        self.count += 1;
        if self.examples.len() < MAX_EXAMPLES {
            self.examples.push(Mismatch { fide_id, ssp, xml });
        }
    }
}

#[derive(Default)]
pub struct DiffReport {
    pub matched: u64,
    pub ssp_only: u64,
    pub xml_only: u64,
    family: Field,
    given: Field,
    suffix: Field,
    federation: Field,
    title: Field,
    female: Field,
    birth_year: Field,
    current_rating: Field,
    aliases: Field,
    peak: Field,
    history: Field,
}

/// Highest rating in `history` and the (year, month) it was *first*
/// reached, walking chronologically — matches the registry's own
/// `<peak>/<achieved>` convention (checked directly against a hand
/// example: FIDE 10245154 peaks at 1750 in 2024-03 and stays there
/// through 2025-12; `achieved` names 2024-03, the first month, not the
/// last).
fn compute_peak(history: &std::collections::BTreeMap<i32, [Option<i32>; 12]>) -> Option<(i32, i32, u32)> {
    let mut best: Option<(i32, i32, u32)> = None;
    for (&year, months) in history {
        for (i, v) in months.iter().enumerate() {
            let Some(v) = *v else { continue };
            let better = match best {
                Some((b, _, _)) => v > b,
                None => true,
            };
            if better {
                best = Some((v, year, (i + 1) as u32));
            }
        }
    }
    best.map(|(v, y, m)| (v, y, m))
}

fn alias_key(name: &crate::ssp::SspName) -> (String, Vec<String>, Option<String>) {
    (name.family.clone(), name.given.clone(), name.suffix.clone())
}

fn xml_alias_key(name: &crate::xmlplayers::XmlName) -> (String, Vec<String>, Option<String>) {
    (name.family.clone(), name.given.clone(), name.suffix.clone())
}

pub fn compare(ssp: &HashMap<u64, SspPlayer>, xml: &HashMap<u64, XmlPlayer>) -> DiffReport {
    let mut report = DiffReport::default();

    for id in xml.keys() {
        if !ssp.contains_key(id) {
            report.xml_only += 1;
        }
    }

    for (id, s) in ssp {
        let Some(x) = xml.get(id) else {
            report.ssp_only += 1;
            continue;
        };
        report.matched += 1;
        let id = *id;

        if s.name.family != x.name.family {
            report.family.record(id, s.name.family.clone(), x.name.family.clone());
        }
        if s.name.given != x.name.given {
            report
                .given
                .record(id, format!("{:?}", s.name.given), format!("{:?}", x.name.given));
        }
        if s.federation != x.federation {
            report.federation.record(
                id,
                format!("{:?}", s.federation),
                format!("{:?}", x.federation),
            );
        }
        if s.titles != x.titles {
            report.title.record(id, format!("{:?}", s.titles), format!("{:?}", x.titles));
        }
        if s.female != x.female {
            report.female.record(id, format!("{}", s.female), format!("{}", x.female));
        }
        if s.name.suffix != x.name.suffix {
            report.suffix.record(
                id,
                format!("{:?}", s.name.suffix),
                format!("{:?}", x.name.suffix),
            );
        }
        if s.birth_year != x.birth_year {
            report
                .birth_year
                .record(id, format!("{:?}", s.birth_year), format!("{:?}", x.birth_year));
        }
        // Compared against the SSP's *derived* latest-history rating, not
        // its `[NNNN]` bracket token — confirmed against real examples
        // that the registry's `<current>` tracks the former, not the
        // latter. See `ssp::SspPlayer::bracket_rating` for why.
        let ssp_latest = crate::ssp::latest_rating(&s.rating_history);
        if ssp_latest != x.current_rating {
            report.current_rating.record(
                id,
                format!("{ssp_latest:?}"),
                format!("{:?}", x.current_rating),
            );
        }

        let mut ssp_aliases: Vec<_> = s.aliases.iter().map(alias_key).collect();
        let mut xml_aliases: Vec<_> = x.aliases.iter().map(xml_alias_key).collect();
        ssp_aliases.sort();
        xml_aliases.sort();
        if ssp_aliases != xml_aliases {
            report.aliases.record(
                id,
                format!("{ssp_aliases:?}"),
                format!("{xml_aliases:?}"),
            );
        }

        let ssp_peak = compute_peak(&s.rating_history);
        let xml_peak = (x.peak_value, x.peak_year, x.peak_month);
        let ssp_peak_norm = ssp_peak.map(|(v, y, m)| (Some(v), Some(y), Some(m))).unwrap_or((None, None, None));
        if ssp_peak_norm != xml_peak {
            report.peak.record(id, format!("{ssp_peak_norm:?}"), format!("{xml_peak:?}"));
        }

        let mut history_differs = false;
        for (year, months) in &s.rating_history {
            match x.rating_history.get(year) {
                Some(xm) if xm == months => {}
                _ => {
                    history_differs = true;
                    break;
                }
            }
        }
        if !history_differs && s.rating_history.len() != x.rating_history.len() {
            history_differs = true;
        }
        if history_differs {
            report.history.record(
                id,
                format!("{} years", s.rating_history.len()),
                format!("{} years", x.rating_history.len()),
            );
        }
    }

    report
}

pub fn print_report(report: &DiffReport) {
    println!(
        "matched (both sides, by FIDE id): {}\nssp-only (no XML record for this FIDE id): {}\nxml-only (no SSP record for this FIDE id): {}\n",
        report.matched, report.ssp_only, report.xml_only
    );

    let fields: [(&str, &Field); 10] = [
        ("family", &report.family),
        ("given", &report.given),
        ("suffix", &report.suffix),
        ("federation", &report.federation),
        ("title", &report.title),
        ("female (sex=F)", &report.female),
        ("birth_year", &report.birth_year),
        ("current_rating", &report.current_rating),
        ("aliases (as a set)", &report.aliases),
        ("peak (value, year, month)", &report.peak),
    ];

    for (label, field) in fields {
        println!("{label}: {} mismatches out of {} matched", field.count, report.matched);
        for ex in &field.examples {
            println!("    fide:{}  ssp={}  xml={}", ex.fide_id, ex.ssp, ex.xml);
        }
    }

    println!(
        "rating history (year->month grid): {} mismatches out of {} matched",
        report.history.count, report.matched
    );
    for ex in &report.history.examples {
        println!("    fide:{}  ssp={}  xml={}", ex.fide_id, ex.ssp, ex.xml);
    }
}
