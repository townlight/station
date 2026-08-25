use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_readiness::{LoudnessProofOutcome, ReceiverAcceptance, SinkLoudnessProof};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{
    OutputLoudnessCaptureFacts, OutputLoudnessProofJob, OutputLoudnessProofJobState, StationStore,
};

const JOB_ID: &str = "11111111-2222-4333-8444-555555555555";
const SECOND_JOB_ID: &str = "11111111-2222-4333-8444-555555555556";
const THIRD_JOB_ID: &str = "11111111-2222-4333-8444-555555555557";
const FOURTH_JOB_ID: &str = "11111111-2222-4333-8444-555555555558";
const CHANNEL_ID: &str = "22222222-3333-4444-8555-666666666666";
const SINK_ID: &str = "33333333-4444-4555-8666-777777777777";
const STREAMING_SINK_ID: &str = "44444444-5555-4666-8777-888888888888";
const CONFIGURATION_HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CAPTURE_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}.db"))
}

fn job(job_id: &str) -> OutputLoudnessProofJob {
    OutputLoudnessProofJob {
        job_id: job_id.into(),
        channel_id: CHANNEL_ID.into(),
        target_group: "cable".into(),
        sink_id: SINK_ID.into(),
        target_lufs_milli: -24_000,
        candidate_version: "0.3.0".into(),
        source_commit: Some(SOURCE_COMMIT.into()),
        configuration_hash: CONFIGURATION_HASH.into(),
        sample_duration_ms: 30_000,
        max_capture_bytes: 1_000_000,
        created_at_unix_ms: 1_000,
    }
}

fn capture() -> OutputLoudnessCaptureFacts {
    OutputLoudnessCaptureFacts {
        artifact_relative_path: "proofs/jobs/cable.ts".into(),
        capture_bytes: 900_000,
        sample_duration_ms: 29_500,
        capture_sha256: CAPTURE_HASH.into(),
    }
}

fn proof(configuration_hash: &str) -> SinkLoudnessProof {
    SinkLoudnessProof {
        proof_id: JOB_ID.into(),
        channel_id: CHANNEL_ID.into(),
        sink_id: SINK_ID.into(),
        target_lufs_milli: -24_000,
        measured_lufs_milli: Some(-23_500),
        tolerance_lufs_milli: 2_000,
        outcome: LoudnessProofOutcome::Pass,
        receiver_acceptance: ReceiverAcceptance::NotAttempted,
        captured_at_unix_ms: 4_000,
        sample_duration_ms: 29_500,
        capture_bytes: 900_000,
        capture_sha256: CAPTURE_HASH.into(),
        candidate_version: "0.3.0".into(),
        source_commit: Some(SOURCE_COMMIT.into()),
        configuration_hash: configuration_hash.into(),
        measurement_engine: "GStreamer ebur128level".into(),
        measurement_method: "decoded post-mux MPEG-TS integrated loudness".into(),
        detail: "Measured from the active target-group transport.".into(),
    }
}

fn configured_channel() -> ChannelConfiguration {
    ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Loudness job channel".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(SINK_ID, "Cable", "127.0.0.1:5500", -24.0),
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

fn configured_job(job_id: &str, configuration_hash: &str) -> OutputLoudnessProofJob {
    let mut job = job(job_id);
    job.configuration_hash = configuration_hash.into();
    job
}

fn configure(store: &StationStore) -> String {
    store.put_channel(&configured_channel()).unwrap();
    store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap()
}

#[test]
fn output_loudness_job_lifecycle_is_cas_and_terminal_facts_are_immutable() {
    let database = temporary_database("loudness-job-lifecycle");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);

    assert!(
        store
            .create_output_loudness_job(&configured_job(JOB_ID, &configuration_hash))
            .unwrap()
    );
    assert!(
        !store
            .create_output_loudness_job(&configured_job(SECOND_JOB_ID, &configuration_hash))
            .unwrap()
    );

    let claimed = store
        .claim_output_loudness_job(JOB_ID, 2_000, 10_000)
        .unwrap()
        .unwrap();
    assert_eq!(claimed.state, OutputLoudnessProofJobState::Claimed);
    assert_eq!(claimed.lease_expires_at_unix_ms, Some(12_000));

    assert!(
        store
            .advance_output_loudness_job(
                JOB_ID,
                OutputLoudnessProofJobState::Pending,
                OutputLoudnessProofJobState::CapturePending,
                3_000,
                10_000,
                None,
            )
            .is_err()
    );
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::Claimed,
            OutputLoudnessProofJobState::CapturePending,
            3_000,
            10_000,
            None,
        )
        .unwrap();
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::CapturePending,
            OutputLoudnessProofJobState::Analyzing,
            4_000,
            10_000,
            Some(&capture()),
        )
        .unwrap();
    let completed = store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::Analyzing,
            OutputLoudnessProofJobState::Completed,
            5_000,
            10_000,
            None,
        )
        .unwrap();
    assert_eq!(completed.state, OutputLoudnessProofJobState::Completed);
    assert_eq!(completed.capture, Some(capture()));
    assert_eq!(completed.completed_at_unix_ms, Some(5_000));
    assert!(
        store
            .fail_output_loudness_job(
                JOB_ID,
                OutputLoudnessProofJobState::Completed,
                6_000,
                "late failure",
            )
            .is_err()
    );

    assert!(
        store
            .create_output_loudness_job(&configured_job(SECOND_JOB_ID, &configuration_hash))
            .unwrap()
    );
    let failed = store
        .claim_output_loudness_job(SECOND_JOB_ID, 7_000, 10_000)
        .unwrap()
        .unwrap();
    assert_eq!(failed.state, OutputLoudnessProofJobState::Claimed);
    let failed = store
        .fail_output_loudness_job(
            SECOND_JOB_ID,
            OutputLoudnessProofJobState::Claimed,
            8_000,
            "capture could not be opened",
        )
        .unwrap();
    assert_eq!(failed.state, OutputLoudnessProofJobState::Failed);
    assert_eq!(
        failed.failure_detail.as_deref(),
        Some("capture could not be opened")
    );
    assert!(
        store
            .fail_output_loudness_job(
                SECOND_JOB_ID,
                OutputLoudnessProofJobState::Claimed,
                9_000,
                "late overwrite",
            )
            .is_err()
    );
    assert!(
        store
            .create_output_loudness_job(&configured_job(THIRD_JOB_ID, &configuration_hash))
            .unwrap()
    );
}

#[test]
fn expired_leases_recover_to_pending_and_can_be_reclaimed_after_restart() {
    let database = temporary_database("loudness-job-recovery");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);
    assert!(
        store
            .create_output_loudness_job(&configured_job(JOB_ID, &configuration_hash))
            .unwrap()
    );
    store
        .claim_output_loudness_job(JOB_ID, 2_000, 10_000)
        .unwrap();
    drop(store);
    let store = StationStore::open(&database).unwrap();
    assert_eq!(
        store.recover_expired_output_loudness_jobs(11_999).unwrap(),
        0
    );
    assert_eq!(
        store.recover_expired_output_loudness_jobs(12_000).unwrap(),
        1
    );
    assert_eq!(
        store
            .read_output_loudness_job(JOB_ID)
            .unwrap()
            .unwrap()
            .state,
        OutputLoudnessProofJobState::Pending
    );
    assert!(
        store
            .claim_output_loudness_job(JOB_ID, 13_000, 10_000)
            .unwrap()
            .is_some()
    );
}

#[test]
fn abandoned_unfinished_jobs_fail_by_state_cas_even_after_lease_expiry() {
    let database = temporary_database("loudness-job-abandonment");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);

    for (job_id, target_state) in [
        (JOB_ID, OutputLoudnessProofJobState::Pending),
        (SECOND_JOB_ID, OutputLoudnessProofJobState::Claimed),
        (THIRD_JOB_ID, OutputLoudnessProofJobState::CapturePending),
        (FOURTH_JOB_ID, OutputLoudnessProofJobState::Analyzing),
    ] {
        store
            .create_output_loudness_job(&configured_job(job_id, &configuration_hash))
            .unwrap();
        if target_state != OutputLoudnessProofJobState::Pending {
            store
                .claim_output_loudness_job(job_id, 2_000, 1_000)
                .unwrap()
                .unwrap();
        }
        if matches!(
            target_state,
            OutputLoudnessProofJobState::CapturePending | OutputLoudnessProofJobState::Analyzing
        ) {
            store
                .advance_output_loudness_job(
                    job_id,
                    OutputLoudnessProofJobState::Claimed,
                    OutputLoudnessProofJobState::CapturePending,
                    2_100,
                    1_000,
                    None,
                )
                .unwrap();
        }
        if target_state == OutputLoudnessProofJobState::Analyzing {
            store
                .advance_output_loudness_job(
                    job_id,
                    OutputLoudnessProofJobState::CapturePending,
                    OutputLoudnessProofJobState::Analyzing,
                    2_200,
                    1_000,
                    Some(&capture()),
                )
                .unwrap();
        }

        let active = store
            .list_unfinished_output_loudness_jobs_for_channel(CHANNEL_ID)
            .unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].job_id, job_id);
        assert_eq!(active[0].state, target_state);

        let failed = store
            .fail_abandoned_output_loudness_job(
                job_id,
                target_state,
                5_000,
                "channel has no active controller",
            )
            .unwrap();
        assert_eq!(failed.state, OutputLoudnessProofJobState::Failed);
        assert_eq!(failed.failed_at_unix_ms, Some(5_000));
        assert_eq!(
            failed.failure_detail.as_deref(),
            Some("channel has no active controller")
        );
        assert!(
            store
                .fail_abandoned_output_loudness_job(job_id, target_state, 6_000, "late overwrite",)
                .is_err()
        );
    }

    assert!(
        store
            .list_unfinished_output_loudness_jobs_for_channel(CHANNEL_ID)
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        store.recover_expired_output_loudness_jobs(7_000).unwrap(),
        0
    );
}

#[test]
fn next_pending_job_is_claimed_in_creation_order_for_one_channel() {
    let database = temporary_database("loudness-job-claim-next");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);

    let mut first = configured_job(JOB_ID, &configuration_hash);
    first.created_at_unix_ms = 2_000;
    store.create_output_loudness_job(&first).unwrap();

    let mut second = configured_job(SECOND_JOB_ID, &configuration_hash);
    second.target_group = "streaming".into();
    second.sink_id = STREAMING_SINK_ID.into();
    second.target_lufs_milli = -16_000;
    second.created_at_unix_ms = 3_000;
    store.create_output_loudness_job(&second).unwrap();

    assert!(
        store
            .claim_next_output_loudness_job("55555555-6666-4777-8888-999999999999", 4_000, 10_000,)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        store
            .claim_next_output_loudness_job(CHANNEL_ID, 4_000, 10_000)
            .unwrap()
            .unwrap()
            .job_id,
        JOB_ID
    );
    assert_eq!(
        store
            .claim_next_output_loudness_job(CHANNEL_ID, 4_001, 10_000)
            .unwrap()
            .unwrap()
            .job_id,
        SECOND_JOB_ID
    );
    assert!(
        store
            .claim_next_output_loudness_job(CHANNEL_ID, 4_002, 10_000)
            .unwrap()
            .is_none()
    );
}

#[test]
fn active_job_lease_renews_only_before_expiry_and_in_the_expected_state() {
    let database = temporary_database("loudness-job-renew-lease");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);
    store
        .create_output_loudness_job(&configured_job(JOB_ID, &configuration_hash))
        .unwrap();
    store
        .claim_output_loudness_job(JOB_ID, 2_000, 10_000)
        .unwrap()
        .unwrap();

    let renewed = store
        .renew_output_loudness_job_lease(
            JOB_ID,
            OutputLoudnessProofJobState::Claimed,
            11_999,
            20_000,
        )
        .unwrap();
    assert_eq!(renewed.lease_expires_at_unix_ms, Some(31_999));
    assert!(
        store
            .renew_output_loudness_job_lease(
                JOB_ID,
                OutputLoudnessProofJobState::CapturePending,
                12_000,
                10_000,
            )
            .is_err()
    );
    assert!(
        store
            .renew_output_loudness_job_lease(
                JOB_ID,
                OutputLoudnessProofJobState::Claimed,
                31_999,
                10_000,
            )
            .is_err()
    );
}

#[test]
fn proof_append_and_job_completion_are_one_atomic_terminal_transition() {
    let database = temporary_database("loudness-job-atomic-completion");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);
    store
        .create_output_loudness_job(&configured_job(JOB_ID, &configuration_hash))
        .unwrap();
    store
        .claim_output_loudness_job(JOB_ID, 2_000, 10_000)
        .unwrap()
        .unwrap();
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::Claimed,
            OutputLoudnessProofJobState::CapturePending,
            2_100,
            10_000,
            None,
        )
        .unwrap();
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::CapturePending,
            OutputLoudnessProofJobState::Analyzing,
            3_000,
            10_000,
            Some(&capture()),
        )
        .unwrap();

    let mut mismatched = proof(&configuration_hash);
    mismatched.capture_sha256 = CONFIGURATION_HASH.into();
    assert!(
        store
            .complete_output_loudness_job_with_proof(
                JOB_ID,
                4_000,
                &mismatched,
                Some("proofs/jobs/cable.ts"),
            )
            .is_err()
    );
    assert_eq!(
        store
            .read_output_loudness_job(JOB_ID)
            .unwrap()
            .unwrap()
            .state,
        OutputLoudnessProofJobState::Analyzing
    );
    assert!(
        store
            .read_latest_sink_loudness_proof(CHANNEL_ID, SINK_ID)
            .unwrap()
            .is_none()
    );

    let completed = store
        .complete_output_loudness_job_with_proof(
            JOB_ID,
            4_001,
            &proof(&configuration_hash),
            Some("proofs/jobs/cable.ts"),
        )
        .unwrap();
    assert_eq!(completed.state, OutputLoudnessProofJobState::Completed);
    assert!(
        store
            .complete_output_loudness_job_with_proof(
                JOB_ID,
                4_002,
                &proof(&configuration_hash),
                Some("proofs/jobs/cable.ts"),
            )
            .is_err()
    );
    assert_eq!(
        store
            .list_sink_loudness_proofs(CHANNEL_ID, Some(SINK_ID), 10)
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn analysis_failure_is_terminal_and_retains_bounded_capture_facts() {
    let database = temporary_database("loudness-job-analysis-failure");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);
    store
        .create_output_loudness_job(&configured_job(JOB_ID, &configuration_hash))
        .unwrap();
    store
        .claim_output_loudness_job(JOB_ID, 2_000, 10_000)
        .unwrap()
        .unwrap();
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::Claimed,
            OutputLoudnessProofJobState::CapturePending,
            2_100,
            10_000,
            None,
        )
        .unwrap();
    store
        .advance_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::CapturePending,
            OutputLoudnessProofJobState::Analyzing,
            3_000,
            10_000,
            Some(&capture()),
        )
        .unwrap();

    let failed = store
        .fail_output_loudness_job(
            JOB_ID,
            OutputLoudnessProofJobState::Analyzing,
            4_000,
            "independent analyzer rejected the captured transport",
        )
        .unwrap();
    assert_eq!(failed.state, OutputLoudnessProofJobState::Failed);
    assert_eq!(failed.capture, Some(capture()));
    assert_eq!(
        store
            .read_output_loudness_job(JOB_ID)
            .unwrap()
            .unwrap()
            .capture,
        Some(capture())
    );
}

#[test]
fn creation_binds_job_to_current_channel_sink_target_and_configuration() {
    let database = temporary_database("loudness-job-authority-binding");
    let store = StationStore::open(&database).unwrap();
    let configuration_hash = configure(&store);

    let accepted = configured_job(JOB_ID, &configuration_hash);
    assert!(store.create_output_loudness_job(&accepted).unwrap());

    let mut stale_hash = configured_job(SECOND_JOB_ID, &configuration_hash);
    stale_hash.configuration_hash = CONFIGURATION_HASH.into();
    stale_hash.target_group = "streaming".into();
    stale_hash.sink_id = STREAMING_SINK_ID.into();
    stale_hash.target_lufs_milli = -16_000;
    assert!(store.create_output_loudness_job(&stale_hash).is_err());

    let mut wrong_target = configured_job(SECOND_JOB_ID, &configuration_hash);
    wrong_target.target_group = "streaming".into();
    wrong_target.target_lufs_milli = -16_000;
    assert!(store.create_output_loudness_job(&wrong_target).is_err());

    let mut missing_sink = configured_job(SECOND_JOB_ID, &configuration_hash);
    missing_sink.sink_id = "55555555-6666-4777-8888-999999999999".into();
    assert!(store.create_output_loudness_job(&missing_sink).is_err());
}

#[test]
fn job_and_capture_validation_rejects_unbounded_or_untrusted_facts() {
    let mut invalid = job(JOB_ID);
    invalid.channel_id = "not-a-uuid".into();
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.target_lufs_milli = -23_000;
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.target_group = "streaming".into();
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.configuration_hash = "not-a-hash".into();
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.source_commit = Some("not-a-commit".into());
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.source_commit = None;
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.sample_duration_ms = 2_999;
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.sample_duration_ms = 600_001;
    assert!(invalid.validate().is_err());

    let mut invalid = job(JOB_ID);
    invalid.max_capture_bytes = 1024 * 1024 * 1024 + 1;
    assert!(invalid.validate().is_err());

    let mut invalid_capture = capture();
    invalid_capture.artifact_relative_path = "../outside.ts".into();
    assert!(invalid_capture.validate(1_000_000, 30_000).is_err());

    let mut invalid_capture = capture();
    invalid_capture.capture_bytes = 1_000_001;
    assert!(invalid_capture.validate(1_000_000, 30_000).is_err());

    let mut invalid_capture = capture();
    invalid_capture.sample_duration_ms = 2_999;
    assert!(invalid_capture.validate(1_000_000, 30_000).is_err());

    let mut invalid_capture = capture();
    invalid_capture.sample_duration_ms = 30_001;
    assert!(invalid_capture.validate(1_000_000, 30_000).is_err());
}
