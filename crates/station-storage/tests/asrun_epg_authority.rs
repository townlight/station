use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_reporting::runtime::{AsRunState, JournalTake, ProjectionInput, ReportingRuntime};
use station_storage::StationStore;

static SEQUENCE: AtomicU64 = AtomicU64::new(0);
const CHANNEL: &str = "00000000-0000-0000-0000-000000000001";

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
        journal_id: "00000000-0000-0000-0000-000000000002".into(),
        journal_sequence: 1,
        channel_id: CHANNEL.into(),
        started_at_unix_ms: 1_700_000_000_000,
        ended_at_unix_ms: 1_700_000_060_000,
        state: AsRunState::Program,
        content_id: "00000000-0000-0000-0000-000000000003".into(),
        franchise_id: "00000000-0000-0000-0000-000000000004".into(),
        category: "program".into(),
        title: "Council".into(),
        catalog_revision: 1,
        schedule_revision: 1,
        journal_hash: "a".repeat(64),
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
