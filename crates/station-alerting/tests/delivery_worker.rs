use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use station_alerting::{AlertDeliveryWorker, AlertTransport, DeliveryEnvelope, DeliveryReceipt};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDeliveryState, AlertEvent,
    AlertEventState, AlertNotificationKind, AlertSeverity, redact_alert_channel_target,
};
use station_storage::{AlertChannelSecret, StationStore};

const ALERT_CHANNEL_ID: &str = "406798db-38d1-4f2f-b447-1f6558135ba0";

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-alerting-{label}-{nonce}.db"))
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

fn arrange_delivery(database: &Path) {
    let store = StationStore::open(database).unwrap();
    let target = "https://alerts.example.org/station";
    store
        .put_alert_channel(
            &AlertChannel {
                channel_id: ALERT_CHANNEL_ID.into(),
                kind: AlertChannelKind::Webhook,
                label: "Primary pager".into(),
                enabled: true,
                target_redacted: redact_alert_channel_target(AlertChannelKind::Webhook, target)
                    .unwrap(),
                credential_configured: true,
                quiet_hours_start_utc: None,
                quiet_hours_end_utc: None,
                created_at_unix_ms: 100,
                updated_at_unix_ms: 100,
            },
            &AlertChannelSecret {
                target: target.into(),
                endpoint: None,
                sender: None,
                identity: None,
                credential: "signing-secret".into(),
            },
        )
        .unwrap();
    let event = store
        .observe_alert(&AlertEvent {
            event_id: "delivery-event".into(),
            condition: AlertConditionKind::OffAir,
            severity: AlertSeverity::Critical,
            state: AlertEventState::Firing,
            resource_ref: "government-channel".into(),
            summary: "Government channel is off air.".into(),
            detail: "Fallback output did not reach the configured sink.".into(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: 1_000,
            last_observed_at_unix_ms: 1_000,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        })
        .unwrap();
    store
        .enqueue_alert_delivery(
            "delivery-job",
            &event,
            ALERT_CHANNEL_ID,
            AlertNotificationKind::Firing,
            1_100,
            1_100,
        )
        .unwrap();
}

struct RecordingTransport {
    envelopes: Mutex<Vec<DeliveryEnvelope>>,
    outcome: Result<DeliveryReceipt, String>,
}

impl AlertTransport for RecordingTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        self.envelopes.lock().unwrap().push(envelope.clone());
        self.outcome.clone()
    }
}

#[test]
fn decrypts_only_after_claim_and_records_a_successful_delivery() {
    let database = database_path("success");
    arrange_delivery(&database);
    let transport = RecordingTransport {
        envelopes: Mutex::new(Vec::new()),
        outcome: Ok(DeliveryReceipt {
            signature: Some("sha256=proof".into()),
        }),
    };
    let worker = AlertDeliveryWorker::new(&database, &transport);
    assert!(worker.run_once(1_100).unwrap());
    let recorded = transport.envelopes.lock().unwrap();
    assert_eq!(recorded.len(), 1);
    assert_eq!(
        recorded[0].secret.target,
        "https://alerts.example.org/station"
    );
    assert_eq!(recorded[0].secret.credential, "signing-secret");
    assert_eq!(
        recorded[0].payload.summary,
        "Government channel is off air."
    );
    drop(recorded);
    let delivery = StationStore::open(&database)
        .unwrap()
        .read_alert_delivery("delivery-job")
        .unwrap()
        .unwrap();
    assert_eq!(delivery.state, AlertDeliveryState::Sent);
    assert_eq!(delivery.attempts, 1);
    assert_eq!(delivery.signature.as_deref(), Some("sha256=proof"));
    remove_database(&database);
}

#[test]
fn records_transport_failure_for_a_later_retry_without_losing_the_job() {
    let database = database_path("failure");
    arrange_delivery(&database);
    let transport = RecordingTransport {
        envelopes: Mutex::new(Vec::new()),
        outcome: Err("provider returned 503".into()),
    };
    let worker = AlertDeliveryWorker::new(&database, &transport);
    assert!(worker.run_once(1_100).unwrap());
    let delivery = StationStore::open(&database)
        .unwrap()
        .read_alert_delivery("delivery-job")
        .unwrap()
        .unwrap();
    assert_eq!(delivery.state, AlertDeliveryState::Pending);
    assert_eq!(delivery.attempts, 1);
    assert_eq!(delivery.next_attempt_at_unix_ms, 121_100);
    assert_eq!(delivery.last_error, "provider returned 503");
    remove_database(&database);
}
