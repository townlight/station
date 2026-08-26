use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::JournalWriter;
use station_media_protocol::{PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope};
use station_reporting::runtime::{AsRunState, JournalTake, ProjectionInput, ReportingRuntime};
use station_storage::StationStore;
use station_storage::project_durable_journal;

static SEQUENCE: AtomicU64 = AtomicU64::new(0);
const CHANNEL: &str = "00000000-0000-4000-8000-000000000001";

fn database() -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-asrun-{}-{}.db",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ))
}
fn projection() -> station_reporting::runtime::AsRunProjectionRuntime {
    let take = JournalTake {
        journal_id: "00000000-0000-4000-8000-000000000002".into(),
        journal_sequence: 1,
        channel_id: CHANNEL.into(),
        started_at_unix_ms: 1_700_000_000_000,
        ended_at_unix_ms: 1_700_000_060_000,
        state: AsRunState::Program,
        content_id: Some("00000000-0000-4000-8000-000000000003".into()),
        franchise_id: Some("00000000-0000-4000-8000-000000000004".into()),
        category: Some("program".into()),
        title: Some("Council".into()),
        catalog_revision: Some(1),
        schedule_revision: Some(1),
        journal_hash: "a".repeat(64),
        unavailable_reason: None,
    };
    ReportingRuntime::new()
        .project(ProjectionInput::new(CHANNEL, "America/Denver", vec![take]).unwrap())
        .unwrap()
}
#[test]
fn durable_projection_replays_idempotently_and_survives_restart() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    let value = projection();
    store
        .persist_asrun_projection(&value, 1_700_000_060_000)
        .unwrap();
    store
        .persist_asrun_projection(&value, 1_700_000_060_000)
        .unwrap();
    assert_eq!(
        store
            .page_asrun_projection(CHANNEL, 0, 1)
            .unwrap()
            .unwrap()
            .entries
            .len(),
        1
    );
    drop(store);
    let reopened = StationStore::open(&path).unwrap();
    let page = reopened
        .page_asrun_projection(CHANNEL, 0, 1)
        .unwrap()
        .unwrap();
    assert_eq!(page.checkpoint_sequence, 1);
    assert_eq!(page.station_timezone, "America/Denver");
}

#[test]
fn durable_journal_adapter_replays_and_keeps_missing_authority_explicit() {
    let path = database();
    let journal = path.with_extension("tlj");
    let mut writer =
        JournalWriter::open(&journal, "00000000-0000-4000-8000-000000000010", CHANNEL).unwrap();
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: "00000000-0000-4000-8000-000000000010".into(),
            channel_id: CHANNEL.into(),
            sequence: 1,
            event: WorkerEvent::ScheduledAssetOnAir {
                report_id: "report".into(),
                schedule_item_id: "00000000-0000-4000-8000-000000000020".into(),
                schedule_item_revision: 1,
                asset_id: "a".repeat(64),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000030".into(),
                actual_on_air_at_unix_ms: 1_700_000_000_000,
            },
        })
        .unwrap();
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: "00000000-0000-4000-8000-000000000010".into(),
            channel_id: CHANNEL.into(),
            sequence: 2,
            event: WorkerEvent::ScheduledAssetOnAir {
                report_id: "report-2".into(),
                schedule_item_id: "00000000-0000-4000-8000-000000000021".into(),
                schedule_item_revision: 1,
                asset_id: "b".repeat(64),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000030".into(),
                // This is a bounded lookahead exactly at the window boundary:
                // it closes the first interval but is never emitted as a row
                // outside the requested half-open window.
                actual_on_air_at_unix_ms: 1_700_000_060_000,
            },
        })
        .unwrap();
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let first = project_durable_journal(
        &store,
        &journal,
        "00000000-0000-4000-8000-000000000040",
        CHANNEL,
        "America/Denver",
        1_700_000_000_000,
        1_700_000_060_000,
        1_700_000_060_000,
    )
    .unwrap();
    assert!(
        first.entries[0]
            .unavailable_reason
            .as_deref()
            .unwrap()
            .contains("schedule")
    );
    assert_eq!(first.entries.len(), 1);
    assert_eq!(first.entries[0].ended_at_unix_ms, 1_700_000_060_000);
    let second = project_durable_journal(
        &store,
        &journal,
        "00000000-0000-4000-8000-000000000040",
        CHANNEL,
        "America/Denver",
        1_700_000_000_000,
        1_700_000_060_000,
        1_700_000_060_000,
    )
    .unwrap();
    assert_eq!(first.entries, second.entries);
}
