use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{
    ExternalReceiverAcceptanceReceipt, ReceiverAcceptanceOutcome, ReceiverEvidenceMethod,
    StationStore,
};

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const RECEIPT_ID: &str = "33333333-4444-4555-8666-777777777777";
const SECOND_RECEIPT_ID: &str = "44444444-5555-4666-8777-888888888888";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
static DATABASE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}-{sequence}.db"))
}

fn channel(destination: &str, enabled_sink: bool) -> ChannelConfiguration {
    let mut sink =
        OutputSinkConfiguration::required_udp(SINK_ID, "Cable headend", destination, -24.0);
    sink.enabled = enabled_sink;
    ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Receiver proof channel".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![sink],
        enabled: false,
    }
}

fn receipt(
    receipt_id: &str,
    configuration_hash: &str,
    outcome: ReceiverAcceptanceOutcome,
    recorded_at_unix_ms: i64,
) -> ExternalReceiverAcceptanceReceipt {
    ExternalReceiverAcceptanceReceipt {
        receipt_id: receipt_id.into(),
        channel_id: CHANNEL_ID.into(),
        sink_id: SINK_ID.into(),
        outcome,
        method: ReceiverEvidenceMethod::HeadendOperatorConfirmation,
        receiver_label: "Cable operator master control".into(),
        evidence_reference: "headend-ticket-1042".into(),
        detail: "Cable operator confirmed stable decoded video and audible program audio.".into(),
        observed_at_unix_ms: recorded_at_unix_ms - 1_000,
        recorded_at_unix_ms,
        recorded_by: "account-setup-admin".into(),
        candidate_version: "0.1.0".into(),
        source_commit: SOURCE_COMMIT.into(),
        configuration_hash: configuration_hash.into(),
    }
}

#[test]
fn current_candidate_and_configuration_receipt_round_trips_as_append_only_history() {
    let database = temporary_database("external-receiver-round-trip");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&channel("192.0.2.40:5500", true))
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let accepted = receipt(
        RECEIPT_ID,
        &configuration_hash,
        ReceiverAcceptanceOutcome::Accepted,
        10_000,
    );
    store
        .append_external_receiver_acceptance_receipt(&accepted)
        .unwrap();
    assert_eq!(
        store
            .read_current_external_receiver_acceptance_receipt(
                CHANNEL_ID,
                SINK_ID,
                "0.1.0",
                SOURCE_COMMIT,
            )
            .unwrap(),
        Some(accepted.clone())
    );

    let rejected = receipt(
        SECOND_RECEIPT_ID,
        &configuration_hash,
        ReceiverAcceptanceOutcome::Rejected,
        10_000,
    );
    store
        .append_external_receiver_acceptance_receipt(&rejected)
        .unwrap();
    assert_eq!(
        store
            .read_current_external_receiver_acceptance_receipt(
                CHANNEL_ID,
                SINK_ID,
                "0.1.0",
                SOURCE_COMMIT,
            )
            .unwrap(),
        Some(rejected)
    );
    let history = store
        .list_external_receiver_acceptance_receipts(CHANNEL_ID, Some(SINK_ID), 10)
        .unwrap();
    assert_eq!(history.len(), 2);
    assert_eq!(history[0].receipt_id, SECOND_RECEIPT_ID);
    assert!(
        store
            .append_external_receiver_acceptance_receipt(&accepted)
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_or_disabled_sink_evidence_cannot_become_current_receiver_acceptance() {
    let database = temporary_database("external-receiver-stale");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&channel("192.0.2.40:5500", true))
        .unwrap();
    let stale_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let stale = receipt(
        RECEIPT_ID,
        &stale_hash,
        ReceiverAcceptanceOutcome::Accepted,
        10_000,
    );

    store
        .put_channel(&channel("192.0.2.41:5500", true))
        .unwrap();
    let error = store
        .append_external_receiver_acceptance_receipt(&stale)
        .unwrap_err();
    assert!(error.contains("configuration"), "{error}");

    let current_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    store
        .put_channel(&channel("192.0.2.41:5500", false))
        .unwrap();
    let disabled = receipt(
        SECOND_RECEIPT_ID,
        &current_hash,
        ReceiverAcceptanceOutcome::Accepted,
        20_000,
    );
    let error = store
        .append_external_receiver_acceptance_receipt(&disabled)
        .unwrap_err();
    assert!(error.contains("enabled sink"), "{error}");

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn candidate_or_configuration_changes_expire_prior_acceptance_without_deleting_history() {
    let database = temporary_database("external-receiver-candidate-boundary");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&channel("192.0.2.40:5500", true))
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    store
        .append_external_receiver_acceptance_receipt(&receipt(
            RECEIPT_ID,
            &configuration_hash,
            ReceiverAcceptanceOutcome::Accepted,
            10_000,
        ))
        .unwrap();

    assert!(
        store
            .read_current_external_receiver_acceptance_receipt(
                CHANNEL_ID,
                SINK_ID,
                "0.2.0",
                SOURCE_COMMIT,
            )
            .unwrap()
            .is_none()
    );
    assert!(
        store
            .read_current_external_receiver_acceptance_receipt(
                CHANNEL_ID,
                SINK_ID,
                "0.1.0",
                "89abcdef0123456789abcdef0123456789abcdef",
            )
            .unwrap()
            .is_none()
    );

    store
        .put_channel(&channel("192.0.2.42:5500", true))
        .unwrap();
    assert!(
        store
            .read_current_external_receiver_acceptance_receipt(
                CHANNEL_ID,
                SINK_ID,
                "0.1.0",
                SOURCE_COMMIT,
            )
            .unwrap()
            .is_none()
    );
    assert_eq!(
        store
            .list_external_receiver_acceptance_receipts(CHANNEL_ID, Some(SINK_ID), 10)
            .unwrap()
            .len(),
        1
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn receipt_validation_rejects_future_observations_and_unbounded_or_missing_evidence() {
    let database = temporary_database("external-receiver-validation");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&channel("192.0.2.40:5500", true))
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let mut future = receipt(
        RECEIPT_ID,
        &configuration_hash,
        ReceiverAcceptanceOutcome::Accepted,
        10_000,
    );
    future.observed_at_unix_ms = 10_001;
    assert!(
        store
            .append_external_receiver_acceptance_receipt(&future)
            .is_err()
    );

    let mut empty_reference = receipt(
        SECOND_RECEIPT_ID,
        &configuration_hash,
        ReceiverAcceptanceOutcome::Accepted,
        20_000,
    );
    empty_reference.evidence_reference.clear();
    assert!(
        store
            .append_external_receiver_acceptance_receipt(&empty_reference)
            .is_err()
    );
    assert!(
        store
            .list_external_receiver_acceptance_receipts(CHANNEL_ID, None, 0)
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}
