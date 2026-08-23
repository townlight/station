use station_readiness::{
    CandidateIdentity, EffectiveLoudnessStatus, LoudnessProofOutcome, ReceiverAcceptance,
    SinkLoudnessProof, derive_loudness_outcome,
};

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const PROOF_ID: &str = "33333333-4444-4555-8666-777777777777";
const CONFIG_HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CAPTURE_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

fn candidate() -> CandidateIdentity {
    CandidateIdentity {
        version: "0.3.0".into(),
        source_commit: Some(SOURCE_COMMIT.into()),
    }
}

fn make_proof(outcome: LoudnessProofOutcome) -> SinkLoudnessProof {
    SinkLoudnessProof {
        proof_id: PROOF_ID.into(),
        channel_id: CHANNEL_ID.into(),
        sink_id: SINK_ID.into(),
        target_lufs_milli: -24_000,
        measured_lufs_milli: match outcome {
            LoudnessProofOutcome::Pass => Some(-23_600),
            LoudnessProofOutcome::Fail => Some(-20_000),
            LoudnessProofOutcome::NotRun | LoudnessProofOutcome::Invalid => None,
        },
        tolerance_lufs_milli: 2_000,
        outcome,
        receiver_acceptance: ReceiverAcceptance::Unknown,
        captured_at_unix_ms: 1_000,
        sample_duration_ms: 30_000,
        capture_bytes: 1_000_000,
        capture_sha256: CAPTURE_HASH.into(),
        candidate_version: "0.3.0".into(),
        source_commit: Some(SOURCE_COMMIT.into()),
        configuration_hash: CONFIG_HASH.into(),
        measurement_engine: "GStreamer 1.28.6".into(),
        measurement_method: "decoded MPEG-TS ebur128 integrated loudness".into(),
        detail: "bounded local UDP capture".into(),
    }
}

#[test]
fn exact_executable_targets_use_default_two_lufs_tolerance() {
    assert_eq!(
        derive_loudness_outcome(-24_000, Some(-23_600), 2_000),
        LoudnessProofOutcome::Pass
    );
    assert_eq!(
        derive_loudness_outcome(-16_000, Some(-14_100), 2_000),
        LoudnessProofOutcome::Pass
    );
    assert_eq!(
        derive_loudness_outcome(-24_000, Some(-20_000), 2_000),
        LoudnessProofOutcome::Fail
    );
    assert_eq!(
        derive_loudness_outcome(-23_000, Some(-23_000), 2_000),
        LoudnessProofOutcome::Invalid
    );
}

#[test]
fn missing_measurement_is_not_run_and_never_passes() {
    assert_eq!(
        derive_loudness_outcome(-24_000, None, 2_000),
        LoudnessProofOutcome::NotRun
    );
    let proof = make_proof(LoudnessProofOutcome::NotRun);
    assert_eq!(proof.validate(), Ok(()));
    assert_eq!(
        proof.effective_status(&candidate(), CONFIG_HASH, 20_000, 60_000),
        EffectiveLoudnessStatus::NotRun
    );
}

#[test]
fn local_pass_with_unknown_receiver_cannot_claim_receiver_acceptance() {
    let proof = make_proof(LoudnessProofOutcome::Pass);
    assert_eq!(proof.validate(), Ok(()));
    assert!(!proof.claims_receiver_acceptance());
    assert_eq!(
        proof.effective_status(&candidate(), CONFIG_HASH, 20_000, 60_000),
        EffectiveLoudnessStatus::Pass
    );
}

#[test]
fn mismatched_candidate_configuration_or_age_is_expired_without_mutating_outcome() {
    let proof = make_proof(LoudnessProofOutcome::Pass);
    assert_eq!(proof.outcome, LoudnessProofOutcome::Pass);
    assert_eq!(
        proof.effective_status(
            &CandidateIdentity {
                version: "0.4.0".into(),
                source_commit: Some(SOURCE_COMMIT.into()),
            },
            CONFIG_HASH,
            20_000,
            60_000,
        ),
        EffectiveLoudnessStatus::Expired
    );
    assert_eq!(
        proof.effective_status(
            &candidate(),
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            20_000,
            60_000
        ),
        EffectiveLoudnessStatus::Expired
    );
    assert_eq!(
        proof.effective_status(&candidate(), CONFIG_HASH, 70_001, 60_000),
        EffectiveLoudnessStatus::Expired
    );
    assert_eq!(proof.outcome, LoudnessProofOutcome::Pass);
}

#[test]
fn absent_verified_source_commit_is_never_current() {
    let proof = make_proof(LoudnessProofOutcome::Pass);
    let candidate_without_commit = CandidateIdentity {
        version: "0.3.0".into(),
        source_commit: None,
    };
    assert_eq!(
        proof.effective_status(&candidate_without_commit, CONFIG_HASH, 20_000, 60_000),
        EffectiveLoudnessStatus::Expired
    );
}

#[test]
fn proof_validation_rejects_noncanonical_identity_hashes_and_unbounded_fields() {
    let mut proof = make_proof(LoudnessProofOutcome::Pass);
    proof.channel_id = "channel".into();
    assert!(proof.validate().is_err());

    let mut proof = make_proof(LoudnessProofOutcome::Pass);
    proof.capture_sha256 = "not-a-sha256".into();
    assert!(proof.validate().is_err());

    let mut proof = make_proof(LoudnessProofOutcome::Pass);
    proof.capture_bytes = 0;
    assert!(proof.validate().is_err());

    let mut proof = make_proof(LoudnessProofOutcome::Pass);
    proof.detail = "x".repeat(4_001);
    assert!(proof.validate().is_err());
}

#[test]
fn proof_validation_rejects_forged_pass_or_fail_outcomes() {
    let mut forged_pass = make_proof(LoudnessProofOutcome::Pass);
    forged_pass.measured_lufs_milli = Some(-20_000);
    assert!(forged_pass.validate().is_err());

    let mut forged_fail = make_proof(LoudnessProofOutcome::Fail);
    forged_fail.measured_lufs_milli = Some(-23_600);
    assert!(forged_fail.validate().is_err());
}

#[test]
fn mixed_targets_remain_independent_proof_contracts() {
    let cable = make_proof(LoudnessProofOutcome::Pass);
    let mut streaming = make_proof(LoudnessProofOutcome::Pass);
    streaming.proof_id = "44444444-5555-4666-8777-888888888888".into();
    streaming.sink_id = "55555555-6666-4777-8888-999999999999".into();
    streaming.target_lufs_milli = -16_000;
    streaming.measured_lufs_milli = Some(-15_700);
    assert_eq!(cable.validate(), Ok(()));
    assert_eq!(streaming.validate(), Ok(()));
    assert_ne!(cable.target_lufs_milli, streaming.target_lufs_milli);
    assert_ne!(cable.measured_lufs_milli, streaming.measured_lufs_milli);
}
