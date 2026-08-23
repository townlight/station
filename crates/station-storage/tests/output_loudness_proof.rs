use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_readiness::{LoudnessProofOutcome, ReceiverAcceptance, SinkLoudnessProof};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::StationStore;

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const CABLE_SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const STREAMING_SINK_ID: &str = "55555555-6666-4777-8888-999999999999";
const CAPTURE_SHA256: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}.db"))
}

fn channel() -> ChannelConfiguration {
    ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Loudness proof channel".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(CABLE_SINK_ID, "Cable", "127.0.0.1:5500", -24.0),
            OutputSinkConfiguration::required_udp(
                STREAMING_SINK_ID,
                "Streaming",
                "127.0.0.1:5501",
                -16.0,
            ),
        ],
        enabled: false,
    }
}

fn proof(
    proof_id: &str,
    sink_id: &str,
    target: i32,
    measured: i32,
    captured_at: i64,
    configuration_hash: &str,
) -> SinkLoudnessProof {
    SinkLoudnessProof {
        proof_id: proof_id.into(),
        channel_id: CHANNEL_ID.into(),
        sink_id: sink_id.into(),
        target_lufs_milli: target,
        measured_lufs_milli: Some(measured),
        tolerance_lufs_milli: 2_000,
        outcome: LoudnessProofOutcome::Pass,
        receiver_acceptance: ReceiverAcceptance::Unknown,
        captured_at_unix_ms: captured_at,
        sample_duration_ms: 30_000,
        capture_bytes: 1_000_000,
        capture_sha256: CAPTURE_SHA256.into(),
        candidate_version: "0.3.0".into(),
        source_commit: Some(SOURCE_COMMIT.into()),
        configuration_hash: configuration_hash.into(),
        measurement_engine: "GStreamer 1.28.6".into(),
        measurement_method: "decoded MPEG-TS ebur128 integrated loudness".into(),
        detail: "bounded local UDP capture".into(),
    }
}

#[test]
fn append_round_trip_preserves_per_sink_proof_and_managed_artifact_metadata() {
    let database = temporary_database("loudness-proof-round-trip");
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel()).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let cable = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &configuration_hash,
    );
    let streaming = proof(
        "44444444-5555-4666-8777-888888888888",
        STREAMING_SINK_ID,
        -16_000,
        -15_700,
        1_001,
        &configuration_hash,
    );
    store
        .append_sink_loudness_proof(&cable, Some("proofs/run-1/cable.ts"))
        .unwrap();
    store
        .append_sink_loudness_proof(&streaming, Some("proofs/run-1/stream.ts"))
        .unwrap();

    let cable = store
        .read_latest_sink_loudness_proof(CHANNEL_ID, CABLE_SINK_ID)
        .unwrap()
        .unwrap();
    let streaming = store
        .read_latest_sink_loudness_proof(CHANNEL_ID, STREAMING_SINK_ID)
        .unwrap()
        .unwrap();
    assert_eq!(
        cable.proof,
        proof(
            "33333333-4444-4555-8666-777777777777",
            CABLE_SINK_ID,
            -24_000,
            -23_600,
            1_000,
            &configuration_hash,
        )
    );
    assert_eq!(
        cable.artifact_relative_path.as_deref(),
        Some("proofs/run-1/cable.ts")
    );
    assert_eq!(streaming.proof.target_lufs_milli, -16_000);
    assert_eq!(streaming.proof.measured_lufs_milli, Some(-15_700));
    assert_eq!(
        streaming.artifact_relative_path.as_deref(),
        Some("proofs/run-1/stream.ts")
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn latest_per_sink_is_deterministic_and_history_remains_append_only() {
    let database = temporary_database("loudness-proof-append-only");
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel()).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let first = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &configuration_hash,
    );
    let second = proof(
        "44444444-5555-4666-8777-888888888888",
        CABLE_SINK_ID,
        -24_000,
        -24_100,
        2_000,
        &configuration_hash,
    );
    store.append_sink_loudness_proof(&first, None).unwrap();
    store.append_sink_loudness_proof(&second, None).unwrap();
    assert_eq!(
        store
            .read_latest_sink_loudness_proof(CHANNEL_ID, CABLE_SINK_ID)
            .unwrap()
            .unwrap()
            .proof
            .proof_id,
        second.proof_id
    );
    assert_eq!(
        store
            .list_sink_loudness_proofs(CHANNEL_ID, Some(CABLE_SINK_ID), 10)
            .unwrap()
            .len(),
        2
    );

    assert!(store.append_sink_loudness_proof(&first, None).is_err());

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn proof_history_survives_sink_reconfiguration_without_fabricated_migration_rows() {
    let database = temporary_database("loudness-proof-reconfiguration");
    let store = StationStore::open(&database).unwrap();
    let mut configured = channel();
    store.put_channel(&configured).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let record = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &configuration_hash,
    );
    store
        .append_sink_loudness_proof(&record, Some("proofs/run-1/cable.ts"))
        .unwrap();

    configured.output_sinks.reverse();
    store.put_channel(&configured).unwrap();
    let stored = store
        .read_latest_sink_loudness_proof(CHANNEL_ID, CABLE_SINK_ID)
        .unwrap()
        .unwrap();
    assert_eq!(stored.proof.sink_id, CABLE_SINK_ID);
    assert_eq!(stored.proof.target_lufs_milli, -24_000);
    assert_eq!(
        stored.artifact_relative_path.as_deref(),
        Some("proofs/run-1/cable.ts")
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn invalid_artifact_paths_and_unbounded_limits_fail_closed() {
    let database = temporary_database("loudness-proof-bounds");
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel()).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let record = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &configuration_hash,
    );
    assert!(
        store
            .append_sink_loudness_proof(&record, Some("..\\outside-proof.ts"))
            .is_err()
    );
    assert!(
        store
            .list_sink_loudness_proofs(CHANNEL_ID, None, 0)
            .is_err()
    );
    assert!(
        store
            .list_sink_loudness_proofs(CHANNEL_ID, None, 1_001)
            .is_err()
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn local_output_proof_cannot_claim_external_receiver_acceptance() {
    let database = temporary_database("loudness-proof-receiver-boundary");
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel()).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let mut record = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &configuration_hash,
    );
    record.receiver_acceptance = ReceiverAcceptance::Verified;

    let error = store.append_sink_loudness_proof(&record, None).unwrap_err();
    assert!(error.contains("receiver acceptance"), "{error}");
    assert!(
        store
            .read_latest_sink_loudness_proof(CHANNEL_ID, CABLE_SINK_ID)
            .unwrap()
            .is_none()
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_configuration_hash_is_rejected_after_sink_reconfiguration() {
    let database = temporary_database("loudness-proof-stale-hash");
    let store = StationStore::open(&database).unwrap();
    let configured = channel();
    store.put_channel(&configured).unwrap();
    let stale_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let mut reconfigured = configured;
    reconfigured.output_sinks[0].destination = "127.0.0.1:5510".into();
    store.put_channel(&reconfigured).unwrap();
    let stale = proof(
        "33333333-4444-4555-8666-777777777777",
        CABLE_SINK_ID,
        -24_000,
        -23_600,
        1_000,
        &stale_hash,
    );
    assert!(store.append_sink_loudness_proof(&stale, None).is_err());
    assert!(
        store
            .read_latest_sink_loudness_proof(CHANNEL_ID, CABLE_SINK_ID)
            .unwrap()
            .is_none()
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn snapshot_returns_latest_proof_for_every_sink_without_a_global_history_cap() {
    let database = temporary_database("loudness-proof-snapshot");
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel()).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();

    let streaming = proof(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        STREAMING_SINK_ID,
        -16_000,
        -15_700,
        1,
        &configuration_hash,
    );
    store.append_sink_loudness_proof(&streaming, None).unwrap();
    for captured_at in 1..=1_001 {
        let proof_id = format!("00000000-0000-4000-8000-{captured_at:012x}");
        let cable = proof(
            &proof_id,
            CABLE_SINK_ID,
            -24_000,
            -23_600,
            captured_at,
            &configuration_hash,
        );
        store.append_sink_loudness_proof(&cable, None).unwrap();
    }

    let snapshot = store
        .read_channel_loudness_proof_snapshot(CHANNEL_ID)
        .unwrap()
        .expect("configured channel has a proof snapshot");
    assert_eq!(
        snapshot
            .output_sinks
            .iter()
            .map(|sink| sink.sink_id.as_str())
            .collect::<Vec<_>>(),
        vec![CABLE_SINK_ID, STREAMING_SINK_ID]
    );
    assert_eq!(snapshot.configuration_hash, configuration_hash);
    assert_eq!(snapshot.latest_proofs.len(), 2);
    assert_eq!(snapshot.latest_proofs[0].proof.sink_id, CABLE_SINK_ID);
    assert_eq!(
        snapshot.latest_proofs[0].proof.proof_id,
        "00000000-0000-4000-8000-0000000003e9"
    );
    assert_eq!(snapshot.latest_proofs[1].proof.sink_id, STREAMING_SINK_ID);
    assert_eq!(
        snapshot.latest_proofs[1].proof.proof_id,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}
