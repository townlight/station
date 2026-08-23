use std::path::PathBuf;

use serde_json::json;
use station_media_journal::inspect_journal;
use station_media_protocol::WorkerEvent;

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let path = arguments.next().map(PathBuf::from).unwrap_or_else(|| {
        eprintln!("usage: inspect_journal <journal-path>");
        std::process::exit(2);
    });
    if arguments.next().is_some() || !path.is_absolute() {
        eprintln!("inspect_journal requires exactly one absolute journal path");
        std::process::exit(2);
    }
    let inspection = inspect_journal(&path).unwrap_or_else(|error| {
        eprintln!("journal validation failed: {error:?}");
        std::process::exit(3);
    });
    let raw_heartbeats = inspection
        .retained_events
        .iter()
        .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .count();
    let compacted_samples = inspection
        .compacted_heartbeat_ranges
        .iter()
        .try_fold(0_u64, |total, range| total.checked_add(range.sample_count))
        .unwrap_or_else(|| {
            eprintln!("compacted heartbeat sample count overflowed");
            std::process::exit(3);
        });
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "path": path,
            "next_sequence": inspection.next_sequence,
            "retained_event_records": inspection.retained_events.len(),
            "retained_non_heartbeat_records": inspection.retained_events.len() - raw_heartbeats,
            "retained_raw_heartbeats": raw_heartbeats,
            "compacted_range_count": inspection.compacted_heartbeat_ranges.len(),
            "compacted_heartbeat_samples": compacted_samples,
            "compacted_ranges": inspection.compacted_heartbeat_ranges,
        }))
        .expect("journal inspection JSON is serializable")
    );
}
