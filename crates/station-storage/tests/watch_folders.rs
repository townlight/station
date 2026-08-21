use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_lifecycle::WatchFolderConfig;
use station_storage::StationStore;

#[test]
fn persists_watch_configuration_scan_health_and_file_settle_state() {
    let database = database_path();
    let store = StationStore::open(&database).unwrap();
    let config = WatchFolderConfig {
        config_id: "11111111-2222-4333-8444-555555555555".into(),
        display_name: "NAS ingest".into(),
        monitor_path: r"\\station-nas\incoming".into(),
        extensions: vec![".mp4".into(), ".ts".into()],
        settle_seconds: 10,
        enabled: true,
        last_scan_at_unix_ms: None,
        last_error: None,
    };
    store.put_watch_folder(&config).unwrap();
    store
        .record_watch_folder_scan(&config.config_id, 100, Some("Share is temporarily offline"))
        .unwrap();
    let listed = store.list_watch_folders().unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].last_scan_at_unix_ms, Some(100));
    assert_eq!(
        listed[0].last_error.as_deref(),
        Some("Share is temporarily offline")
    );

    let path = r"\\station-nas\incoming\meeting.ts";
    let first = store
        .observe_watch_file(&config.config_id, path, 42, 90, 100)
        .unwrap();
    assert_eq!(first.observation_count, 1);
    assert_eq!(first.unchanged_since_unix_ms, 100);
    let stable = store
        .observe_watch_file(&config.config_id, path, 42, 90, 111)
        .unwrap();
    assert_eq!(stable.observation_count, 2);
    assert_eq!(stable.unchanged_since_unix_ms, 100);
    store
        .mark_watch_file_queued(
            &config.config_id,
            path,
            "22222222-3333-4444-8555-666666666666",
        )
        .unwrap();

    let reopened = StationStore::open(&database).unwrap();
    let queued = reopened
        .observe_watch_file(&config.config_id, path, 42, 90, 120)
        .unwrap();
    assert_eq!(queued.observation_count, 3);
    assert_eq!(
        queued.queued_job_id.as_deref(),
        Some("22222222-3333-4444-8555-666666666666")
    );
    let changed = reopened
        .observe_watch_file(&config.config_id, path, 43, 121, 122)
        .unwrap();
    assert_eq!(changed.observation_count, 1);
    assert_eq!(changed.queued_job_id, None);

    drop(reopened);
    drop(store);
    let _ = std::fs::remove_file(database);
}

fn database_path() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-watch-storage-{nonce}.db"))
}
