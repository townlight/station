use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState,
};
use station_schedule::ChannelConfiguration;
use station_storage::StationStore;

const CHANNEL_ID: &str = "8ea48bc4-7f2e-4f89-9bbc-2c58472f0e8e";

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{label}-{nonce}.db"))
}

fn remove_database(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

fn alert(event_id: &str, observed_at_unix_ms: i64) -> AlertEvent {
    AlertEvent {
        event_id: event_id.into(),
        condition: AlertConditionKind::EncoderDeath,
        severity: AlertSeverity::Critical,
        state: AlertEventState::Firing,
        resource_ref: CHANNEL_ID.into(),
        summary: "Channel worker exited unexpectedly.".into(),
        detail: "The supervisor will restart it and keep fallback on air.".into(),
        source_section: "S8".into(),
        first_observed_at_unix_ms: observed_at_unix_ms,
        last_observed_at_unix_ms: observed_at_unix_ms,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    }
}

#[test]
fn persists_monotonic_channel_runtime_truth() {
    let database = database_path("channel-runtime");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Government Channel".into(),
            udp_destination: "127.0.0.1:5500".into(),
            enabled: true,
        })
        .unwrap();
    let fallback = ChannelRuntimeObservation {
        channel_id: CHANNEL_ID.into(),
        state: ChannelRuntimeState::HealthyFallback,
        on_air_asset_id: None,
        state_started_at_unix_ms: 1_000,
        heartbeat_at_unix_ms: 2_000,
        detail: None,
    };
    store.put_channel_runtime(&fallback).unwrap();
    let mut stale = fallback.clone();
    stale.state = ChannelRuntimeState::Error;
    stale.heartbeat_at_unix_ms = 1_999;
    stale.detail = Some("stale write".into());
    assert!(store.put_channel_runtime(&stale).is_err());
    assert_eq!(
        store.read_channel_runtime(CHANNEL_ID).unwrap(),
        Some(fallback.clone())
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.list_channel_runtime().unwrap(), vec![fallback]);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn deduplicates_acknowledges_and_resolves_alerts_durably() {
    let database = database_path("alerts");
    let store = StationStore::open(&database).unwrap();
    let first = store.observe_alert(&alert("alert-first", 1_000)).unwrap();
    assert_eq!(first.occurrence_count, 1);

    let repeated = store.observe_alert(&alert("alert-ignored", 2_000)).unwrap();
    assert_eq!(repeated.event_id, first.event_id);
    assert_eq!(repeated.occurrence_count, 2);
    assert_eq!(store.list_alert_events().unwrap().len(), 1);

    let acknowledged = store
        .acknowledge_alert(&first.event_id, 2_100, "station-operator")
        .unwrap()
        .unwrap();
    assert_eq!(
        acknowledged.acknowledged_by.as_deref(),
        Some("station-operator")
    );
    let resolved = store
        .resolve_alert(AlertConditionKind::EncoderDeath, CHANNEL_ID, 3_000)
        .unwrap()
        .unwrap();
    assert_eq!(resolved.state, AlertEventState::Resolved);

    let fresh = store.observe_alert(&alert("alert-fresh", 4_000)).unwrap();
    assert_eq!(fresh.event_id, "alert-fresh");
    assert_eq!(store.list_alert_events().unwrap().len(), 2);
    drop(store);
    remove_database(&database);
}
