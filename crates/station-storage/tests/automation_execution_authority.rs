use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_automation_policy::{
    AdapterLocation, AutomationAdapterDescriptor, AutomationCandidateIdentity,
    AutomationConnectionTestEvidence, AutomationExecutionEngine, AutomationExecutionReceipt,
    AutomationExecutionRequest, AutomationFeature, AutomationFeaturePolicy,
    AutomationInputArtifact, AutomationInputKind, AutomationOutputArtifact, AutomationOutputFacts,
    ConnectionTestOutcome, CredentialAvailability,
};
use station_storage::{AutomationExecutionJobState, StationStore};

const ADAPTER_ID: &str = "11111111-2222-4333-8444-555555555555";
const POLICY_ID: &str = "22222222-2222-4333-8444-555555555555";
const JOB_ID: &str = "33333333-2222-4333-8444-555555555555";
const RECEIPT_ID: &str = "44444444-2222-4333-8444-555555555555";
const CONFIGURATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const INPUT_SHA: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const OUTPUT_SHA: &str = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
const RUNTIME_SHA: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const MODEL_SHA: &str = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";
const COMMIT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static DATABASE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}-{sequence}.db"))
}

fn candidate() -> AutomationCandidateIdentity {
    AutomationCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: COMMIT.into(),
    }
}

fn adapter(revision: u64) -> AutomationAdapterDescriptor {
    AutomationAdapterDescriptor {
        adapter_id: ADAPTER_ID.into(),
        location: AdapterLocation::Local,
        model_key: "townlight-summary-default".into(),
        model_identifier: "provider/model-v1".into(),
        supported_features: vec![AutomationFeature::Summaries],
        credential_availability: CredentialAvailability::NotRequired,
        revision,
    }
}

fn policy(revision: u64) -> AutomationFeaturePolicy {
    AutomationFeaturePolicy {
        policy_id: POLICY_ID.into(),
        feature: AutomationFeature::Summaries,
        operator_selected_adapter_id: Some(ADAPTER_ID.into()),
        adaptive_default: None,
        configuration_hash: CONFIGURATION.into(),
        revision,
    }
}

fn request() -> AutomationExecutionRequest {
    AutomationExecutionRequest {
        job_id: JOB_ID.into(),
        feature: AutomationFeature::Summaries,
        policy_id: POLICY_ID.into(),
        policy_revision: 1,
        adapter_id: ADAPTER_ID.into(),
        adapter_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        input: AutomationInputArtifact {
            kind: AutomationInputKind::Transcript,
            artifact_sha256: INPUT_SHA.into(),
            bytes: 4_096,
            media_type: "text/plain; charset=utf-8".into(),
            language: Some("en-US".into()),
        },
        requested_output_language: None,
        requested_at_unix_ms: 100,
    }
}

fn receipt() -> AutomationExecutionReceipt {
    AutomationExecutionReceipt {
        receipt_id: RECEIPT_ID.into(),
        job_id: JOB_ID.into(),
        feature: AutomationFeature::Summaries,
        policy_id: POLICY_ID.into(),
        policy_revision: 1,
        adapter_id: ADAPTER_ID.into(),
        adapter_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        engine: AutomationExecutionEngine::Local {
            executable_sha256: RUNTIME_SHA.into(),
            model_artifact_sha256: MODEL_SHA.into(),
        },
        input: request().input,
        output: AutomationOutputArtifact {
            artifact_sha256: OUTPUT_SHA.into(),
            bytes: 512,
            media_type: "text/plain; charset=utf-8".into(),
            language: Some("en-US".into()),
        },
        facts: AutomationOutputFacts::Summary {
            input_units: 700,
            output_words: 82,
        },
        started_at_unix_ms: 200,
        completed_at_unix_ms: 300,
    }
}

fn connection_evidence() -> AutomationConnectionTestEvidence {
    AutomationConnectionTestEvidence {
        evidence_id: "55555555-2222-4333-8444-555555555555".into(),
        policy_id: POLICY_ID.into(),
        adapter_id: ADAPTER_ID.into(),
        policy_revision: 1,
        adapter_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 50,
        expires_at_unix_ms: 10_000,
        detail: "Trusted adapter connection test passed.".into(),
    }
}

fn configured_store(name: &str) -> (PathBuf, StationStore) {
    let database = temporary_database(name);
    let store = StationStore::open(&database).unwrap();
    store
        .write_automation_adapter_descriptor(&adapter(1), None, "setup-admin", 10)
        .unwrap();
    store
        .write_automation_feature_policy(&policy(1), None, "setup-admin", 20)
        .unwrap();
    store
        .append_automation_connection_test_evidence(&connection_evidence(), &candidate(), 50)
        .unwrap();
    (database, store)
}

#[test]
fn exact_job_claim_and_receipt_complete_atomically() {
    let (database, store) = configured_store("automation-execution-round-trip");

    let pending = store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(pending.attempt_count, 0);

    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();
    assert_eq!(claimed.state, AutomationExecutionJobState::Claimed);
    assert_eq!(claimed.attempt_count, 1);
    assert_eq!(claimed.lease_expires_at_unix_ms, Some(1_150));

    let completed = store
        .complete_automation_execution_job(
            JOB_ID,
            "automation-worker-1",
            &receipt(),
            &candidate(),
            350,
        )
        .unwrap();
    assert_eq!(completed.state, AutomationExecutionJobState::Completed);
    assert_eq!(completed.receipt, Some(receipt()));
    assert_eq!(
        store.read_automation_execution_job(JOB_ID).unwrap(),
        Some(completed)
    );

    drop(store);
    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened
            .read_automation_execution_job(JOB_ID)
            .unwrap()
            .unwrap()
            .receipt,
        Some(receipt())
    );
    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_authority_wrong_owner_and_expired_lease_fail_closed() {
    let (database, store) = configured_store("automation-execution-rejections");
    let mut stale = request();
    stale.configuration_hash = OUTPUT_SHA.into();
    assert!(
        store
            .enqueue_automation_execution_job(&stale, &candidate(), 100)
            .is_err()
    );

    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 100)
        .unwrap();
    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-2",
                &receipt(),
                &candidate(),
                200,
            )
            .is_err()
    );
    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-1",
                &receipt(),
                &candidate(),
                250,
            )
            .is_err()
    );
    let reclaimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-2", &candidate(), 250, 1_000)
        .unwrap();
    assert_eq!(reclaimed.attempt_count, 2);
    assert_eq!(
        reclaimed.lease_owner.as_deref(),
        Some("automation-worker-2")
    );
    assert_eq!(reclaimed.lease_expires_at_unix_ms, Some(1_250));

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn enqueue_requires_current_passing_connection_evidence() {
    let database = temporary_database("automation-execution-connection-gate");
    let store = StationStore::open(&database).unwrap();
    store
        .write_automation_adapter_descriptor(&adapter(1), None, "setup-admin", 10)
        .unwrap();
    store
        .write_automation_feature_policy(&policy(1), None, "setup-admin", 20)
        .unwrap();

    assert!(
        store
            .enqueue_automation_execution_job(&request(), &candidate(), 100)
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn policy_change_after_claim_blocks_receipt_completion() {
    let (database, store) = configured_store("automation-execution-stale-completion");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();
    store
        .write_automation_feature_policy(&policy(2), Some(1), "setup-admin", 180)
        .unwrap();

    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-1",
                &receipt(),
                &candidate(),
                350,
            )
            .is_err()
    );
    assert_eq!(
        store
            .read_automation_execution_job(JOB_ID)
            .unwrap()
            .unwrap()
            .state,
        AutomationExecutionJobState::Claimed
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn live_owner_can_renew_but_expired_or_wrong_owner_cannot() {
    let (database, store) = configured_store("automation-execution-renewal");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 100)
        .unwrap();

    assert!(
        store
            .renew_automation_execution_job_lease(
                JOB_ID,
                "automation-worker-2",
                &candidate(),
                200,
                1_000,
            )
            .is_err()
    );
    let renewed = store
        .renew_automation_execution_job_lease(
            JOB_ID,
            "automation-worker-1",
            &candidate(),
            200,
            1_000,
        )
        .unwrap();
    assert_eq!(renewed.lease_expires_at_unix_ms, Some(1_200));

    assert!(
        store
            .renew_automation_execution_job_lease(
                JOB_ID,
                "automation-worker-1",
                &candidate(),
                1_200,
                1_000,
            )
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}
