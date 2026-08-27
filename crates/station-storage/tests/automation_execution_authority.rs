use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use sha2::{Digest, Sha256};
use station_automation_policy::{
    AdapterLocation, AutomationAdapterDescriptor, AutomationCandidateIdentity,
    AutomationConnectionTestEvidence, AutomationExecutionEngine, AutomationExecutionReceipt,
    AutomationExecutionRequest, AutomationFeature, AutomationFeaturePolicy,
    AutomationInputArtifact, AutomationInputKind, AutomationOutputArtifact, AutomationOutputFacts,
    ConnectionTestOutcome, CredentialAvailability,
};
use station_maintenance::{VerifiedInstallAuthority, verified_install_authority_for_test};
use station_storage::{
    AutomationExecutionAdapter, AutomationExecutionJobRecord, AutomationExecutionJobState,
    AutomationExecutionSetupFault, StationStore, run_automation_execution_controller_iteration,
    run_automation_execution_controller_iteration_with_stop,
    run_automation_execution_controller_iteration_with_stop_and_elapsed,
};

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

const SQLITE_OK: c_int = 0;
const SQLITE_OPEN_READWRITE: c_int = 2;

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut c_void,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_exec(
        database: *mut c_void,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut c_void) -> c_int;
}

fn raw_sql_status(database_path: &Path, sql: &str) -> c_int {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    // SAFETY: inputs are live C strings and the opened handle is closed below.
    let open = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut database,
            SQLITE_OPEN_READWRITE,
            ptr::null(),
        )
    };
    assert_eq!(open, SQLITE_OK);
    // SAFETY: the handle and SQL string are live, and no callback is used.
    let status = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: this handle was opened above and has not yet been closed.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    status
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}-{sequence}.db"))
}

fn candidate_identity() -> AutomationCandidateIdentity {
    AutomationCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: COMMIT.into(),
    }
}

fn candidate() -> VerifiedInstallAuthority {
    verified_install_authority_for_test("0.1.0", COMMIT, 7).unwrap()
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
        candidate: candidate_identity(),
        install_generation: 7,
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
        candidate: candidate_identity(),
        install_generation: 7,
        claim_id: "77777777-2222-4333-8444-555555555555".into(),
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

fn receipt_for(claimed: &AutomationExecutionJobRecord) -> AutomationExecutionReceipt {
    let mut receipt = receipt();
    receipt.claim_id = claimed.claim_id.clone().unwrap();
    receipt.install_generation = claimed.request.install_generation;
    receipt.started_at_unix_ms = receipt
        .started_at_unix_ms
        .max(claimed.claimed_at_unix_ms.unwrap());
    receipt
}

fn connection_evidence() -> AutomationConnectionTestEvidence {
    AutomationConnectionTestEvidence {
        evidence_id: "55555555-2222-4333-8444-555555555555".into(),
        policy_id: POLICY_ID.into(),
        adapter_id: ADAPTER_ID.into(),
        policy_revision: 1,
        adapter_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate_identity(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 50,
        expires_at_unix_ms: 80_000_000,
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
        .append_automation_connection_test_evidence(
            &connection_evidence(),
            &candidate_identity(),
            50,
        )
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

    let exact_receipt = receipt_for(&claimed);
    let completed = store
        .complete_automation_execution_job(
            JOB_ID,
            "automation-worker-1",
            &exact_receipt,
            &candidate(),
            350,
        )
        .unwrap();
    assert_eq!(completed.state, AutomationExecutionJobState::Completed);
    assert_eq!(completed.receipt, Some(exact_receipt.clone()));
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
        Some(exact_receipt)
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
    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 100)
        .unwrap();
    let exact_receipt = receipt_for(&claimed);
    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-2",
                &exact_receipt,
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
                &exact_receipt,
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
    let claimed = store
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
                &receipt_for(&claimed),
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
    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 100)
        .unwrap();

    assert!(
        store
            .renew_automation_execution_job_lease(
                JOB_ID,
                "automation-worker-2",
                claimed.claim_id.as_deref().unwrap(),
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
            claimed.claim_id.as_deref().unwrap(),
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
                claimed.claim_id.as_deref().unwrap(),
                &candidate(),
                1_200,
                1_000,
            )
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_claim_cannot_renew_or_fail_a_reclaimed_job_under_the_same_worker_identity() {
    let (database, store) = configured_store("automation-execution-stale-claim-capability");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let first = store
        .claim_automation_execution_job(JOB_ID, "stable-controller", &candidate(), 150, 100)
        .unwrap();
    let second = store
        .claim_automation_execution_job(JOB_ID, "stable-controller", &candidate(), 250, 1_000)
        .unwrap();
    let first_claim = first.claim_id.as_deref().unwrap();
    let second_claim = second.claim_id.as_deref().unwrap();
    assert_ne!(first_claim, second_claim);

    assert!(
        store
            .renew_automation_execution_job_lease(
                JOB_ID,
                "stable-controller",
                first_claim,
                &candidate(),
                251,
                1_000,
            )
            .is_err()
    );
    assert!(
        store
            .record_automation_execution_attempt_failure(
                JOB_ID,
                "stable-controller",
                first_claim,
                "adapter_timeout",
                true,
                251,
            )
            .is_err()
    );
    let live = store
        .renew_automation_execution_job_lease(
            JOB_ID,
            "stable-controller",
            second_claim,
            &candidate(),
            251,
            1_000,
        )
        .unwrap();
    assert_eq!(live.claim_id.as_deref(), Some(second_claim));
    assert_eq!(live.lease_expires_at_unix_ms, Some(1_251));

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn durable_job_bindings_and_terminal_rows_are_immutable() {
    let (database, store) = configured_store("automation-execution-job-immutability");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();

    drop(store);
    assert_ne!(
        raw_sql_status(
            &database,
            "UPDATE automation_execution_jobs SET input_sha256='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'"
        ),
        SQLITE_OK,
        "durable request bindings must be immutable"
    );

    let store = StationStore::open(&database).unwrap();
    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();
    store
        .complete_automation_execution_job(
            JOB_ID,
            "automation-worker-1",
            &receipt_for(&claimed),
            &candidate(),
            350,
        )
        .unwrap();
    drop(store);

    assert_ne!(
        raw_sql_status(
            &database,
            "UPDATE automation_execution_jobs SET state='failed',receipt_id='',last_failure_code='tampered'"
        ),
        SQLITE_OK,
        "terminal job rows must be immutable"
    );
    assert_ne!(
        raw_sql_status(&database, "DELETE FROM automation_execution_jobs"),
        SQLITE_OK,
        "durable job history must not be deletable"
    );

    let _ = std::fs::remove_file(database);
}

#[test]
fn v1_execution_schema_migrates_without_losing_pending_job_authority() {
    let (database, store) = configured_store("automation-execution-v1-migration");
    let pending = store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    drop(store);

    assert_eq!(
        raw_sql_status(
            &database,
            "DROP INDEX automation_execution_jobs_pending;\
             ALTER TABLE automation_execution_jobs DROP COLUMN next_attempt_at_unix_ms;\
             CREATE INDEX automation_execution_jobs_pending \
               ON automation_execution_jobs(state,created_at_unix_ms,job_id)",
        ),
        SQLITE_OK,
        "the fixture must be an authentic pre-retry v1 schema"
    );

    let reopened = StationStore::open(&database).expect("v1 schema must migrate exactly once");
    assert_eq!(
        reopened
            .read_automation_execution_job(JOB_ID)
            .expect("read migrated durable job"),
        Some(pending)
    );
    let claimed = reopened
        .claim_automation_execution_job(JOB_ID, "migration-worker", &candidate(), 150, 1_000)
        .expect("migrated job remains claimable");
    assert_eq!(claimed.next_attempt_at_unix_ms, 150);
    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn malformed_execution_schema_is_rejected_instead_of_silently_repaired() {
    let (database, store) = configured_store("automation-execution-malformed-schema");
    drop(store);
    assert_eq!(
        raw_sql_status(
            &database,
            "DROP INDEX automation_execution_jobs_pending;\
             CREATE INDEX automation_execution_jobs_pending \
               ON automation_execution_jobs(state,created_at_unix_ms,job_id)",
        ),
        SQLITE_OK
    );
    assert!(
        StationStore::open(&database).is_err(),
        "an ambiguous schema must fail closed rather than be treated as a safe v1 migration"
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn reopening_rejects_same_name_noop_trigger_and_consistent_authority_rebind() {
    let (database, store) = configured_store("automation-execution-schema-drift");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    drop(store);

    let mut rebound = request();
    rebound.input.artifact_sha256 = "e".repeat(64);
    let rebound_json = serde_json::to_string(&rebound).unwrap();
    let corruption = format!(
        "DROP TRIGGER automation_execution_jobs_binding_immutable;\
         CREATE TRIGGER automation_execution_jobs_binding_immutable \
           BEFORE UPDATE OF input_sha256,request_json ON automation_execution_jobs \
           BEGIN SELECT 1; END;\
         UPDATE automation_execution_jobs SET input_sha256='{}',request_json='{}'",
        rebound.input.artifact_sha256, rebound_json
    );
    assert_eq!(raw_sql_status(&database, &corruption), SQLITE_OK);
    assert!(
        StationStore::open(&database).is_err(),
        "same-name no-op trigger drift must fail before consistent forged authority is readable"
    );

    let _ = std::fs::remove_file(database);
}

#[test]
fn reopening_rejects_deleted_trigger_after_consistent_authority_rebind() {
    let (database, store) = configured_store("automation-execution-deleted-trigger-drift");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    drop(store);

    let mut rebound = request();
    rebound.input.artifact_sha256 = "e".repeat(64);
    let rebound_json = serde_json::to_string(&rebound).unwrap();
    let corruption = format!(
        "DROP TRIGGER automation_execution_jobs_binding_immutable;\
         UPDATE automation_execution_jobs SET input_sha256='{}',request_json='{}'",
        rebound.input.artifact_sha256, rebound_json
    );
    assert_eq!(raw_sql_status(&database, &corruption), SQLITE_OK);
    assert!(
        StationStore::open(&database).is_err(),
        "an established schema must be attested before a missing trigger can be recreated"
    );

    let _ = std::fs::remove_file(database);
}

#[test]
fn reopening_rejects_trigger_literal_case_drift() {
    let (database, store) = configured_store("automation-execution-trigger-case-drift");
    drop(store);
    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER automation_execution_jobs_no_delete;\
             CREATE TRIGGER automation_execution_jobs_no_delete BEFORE DELETE ON automation_execution_jobs \
             BEGIN SELECT RAISE(ABORT, 'AUTOMATION EXECUTION JOBS ARE DURABLE HISTORY'); END"
        ),
        SQLITE_OK
    );
    assert!(StationStore::open(&database).is_err());
    let _ = std::fs::remove_file(database);
}

#[test]
fn reopening_rejects_trigger_literal_embedded_semicolon_drift() {
    let (database, store) = configured_store("automation-execution-trigger-semicolon-drift");
    drop(store);
    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER automation_execution_jobs_no_delete;\
             CREATE TRIGGER automation_execution_jobs_no_delete BEFORE DELETE ON automation_execution_jobs \
             BEGIN SELECT RAISE(ABORT, 'automation; execution jobs are durable history'); END"
        ),
        SQLITE_OK
    );
    assert!(StationStore::open(&database).is_err());
    let _ = std::fs::remove_file(database);
}

#[test]
fn read_fails_closed_when_a_receipt_is_orphaned_from_its_job_row() {
    let (database, store) = configured_store("automation-execution-orphan-receipt");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();
    store
        .complete_automation_execution_job(
            JOB_ID,
            "automation-worker-1",
            &receipt_for(&claimed),
            &candidate(),
            350,
        )
        .unwrap();
    drop(store);

    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER automation_execution_jobs_terminal_immutable;\
             UPDATE automation_execution_jobs SET state='pending',receipt_id='',completed_at_unix_ms=-1;\
             CREATE TRIGGER automation_execution_jobs_terminal_immutable \
               BEFORE UPDATE ON automation_execution_jobs \
               WHEN OLD.state IN ('completed','failed') \
               BEGIN SELECT RAISE(ABORT, 'terminal automation execution jobs are immutable'); END"
        ),
        SQLITE_OK
    );
    let reopened = StationStore::open(&database).unwrap();
    assert!(
        reopened.read_automation_execution_job(JOB_ID).is_err(),
        "a receipt must remain discoverable by job identity even if its pointer is corrupted"
    );

    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn attempt_failures_retry_only_under_the_live_owner_and_can_finish_terminally() {
    let (database, store) = configured_store("automation-execution-attempt-failure");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let first_claim = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();

    assert!(
        store
            .record_automation_execution_attempt_failure(
                JOB_ID,
                "automation-worker-2",
                first_claim.claim_id.as_deref().unwrap(),
                "adapter_timeout",
                true,
                200,
            )
            .is_err()
    );
    let pending = store
        .record_automation_execution_attempt_failure(
            JOB_ID,
            "automation-worker-1",
            first_claim.claim_id.as_deref().unwrap(),
            "adapter_timeout",
            true,
            200,
        )
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(pending.attempt_count, 1);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_timeout")
    );
    assert!(pending.lease_owner.is_none());
    assert!(pending.completed_at_unix_ms.is_none());
    assert_eq!(pending.next_attempt_at_unix_ms, 60_200);

    let second_claim = store
        .claim_automation_execution_job(
            JOB_ID,
            "automation-worker-2",
            &candidate(),
            pending.next_attempt_at_unix_ms,
            1_000,
        )
        .unwrap();
    let failed = store
        .record_automation_execution_attempt_failure(
            JOB_ID,
            "automation-worker-2",
            second_claim.claim_id.as_deref().unwrap(),
            "invalid_provider_response",
            false,
            60_250,
        )
        .unwrap();
    assert_eq!(failed.state, AutomationExecutionJobState::Failed);
    assert_eq!(failed.attempt_count, 2);
    assert_eq!(
        failed.last_failure_code.as_deref(),
        Some("invalid_provider_response")
    );
    assert_eq!(failed.completed_at_unix_ms, Some(60_250));
    assert!(failed.receipt.is_none());
    assert!(
        store
            .record_automation_execution_attempt_failure(
                JOB_ID,
                "automation-worker-2",
                second_claim.claim_id.as_deref().unwrap(),
                "retry_after_terminal",
                true,
                60_251,
            )
            .is_err()
    );

    drop(store);
    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened
            .read_automation_execution_job(JOB_ID)
            .unwrap()
            .unwrap(),
        failed
    );
    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn next_claim_is_fifo_and_recovers_an_expired_lease_after_restart() {
    let (database, store) = configured_store("automation-execution-next-claim");
    let mut second_request = request();
    second_request.job_id = "66666666-2222-4333-8444-555555555555".into();
    second_request.requested_at_unix_ms = 101;
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    store
        .enqueue_automation_execution_job(&second_request, &candidate(), 101)
        .unwrap();

    let first = store
        .claim_next_automation_execution_job("automation-worker-1", &candidate(), 150, 1_000)
        .unwrap()
        .unwrap();
    assert_eq!(first.request.job_id, JOB_ID);
    let second = store
        .claim_next_automation_execution_job("automation-worker-2", &candidate(), 151, 1_000)
        .unwrap()
        .unwrap();
    assert_eq!(second.request.job_id, second_request.job_id);
    assert!(
        store
            .claim_next_automation_execution_job("automation-worker-3", &candidate(), 152, 1_000,)
            .unwrap()
            .is_none()
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let recovered = reopened
        .claim_next_automation_execution_job("automation-worker-3", &candidate(), 1_150, 1_000)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.request.job_id, JOB_ID);
    assert_eq!(recovered.attempt_count, 2);
    assert_eq!(
        recovered.lease_owner.as_deref(),
        Some("automation-worker-3")
    );

    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn expired_tenth_attempt_is_terminalized_instead_of_wedging_the_queue() {
    let (database, store) = configured_store("automation-execution-attempt-ceiling");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();

    let mut now = 150_i64;
    for attempt in 0..10_i64 {
        let claimed = store
            .claim_next_automation_execution_job("automation-worker-1", &candidate(), now, 10)
            .unwrap()
            .unwrap();
        assert_eq!(claimed.attempt_count, u32::try_from(attempt + 1).unwrap());
        if attempt < 9 {
            let pending = store
                .record_automation_execution_attempt_failure(
                    JOB_ID,
                    "automation-worker-1",
                    claimed.claim_id.as_deref().unwrap(),
                    "adapter_timeout",
                    true,
                    now + 1,
                )
                .unwrap();
            assert_eq!(pending.state, AutomationExecutionJobState::Pending);
            now = pending.next_attempt_at_unix_ms;
        }
    }

    assert!(
        store
            .claim_next_automation_execution_job(
                "automation-worker-2",
                &candidate(),
                now + 10,
                1_000,
            )
            .unwrap()
            .is_none()
    );
    let terminal = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(terminal.state, AutomationExecutionJobState::Failed);
    assert_eq!(terminal.attempt_count, 10);
    assert_eq!(
        terminal.last_failure_code.as_deref(),
        Some("attempt_limit_exhausted")
    );
    assert_eq!(terminal.completed_at_unix_ms, Some(now + 10));

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn install_generation_and_live_claim_interval_are_required_for_completion() {
    let (database, store) = configured_store("automation-execution-generation-claim");
    let reactivated = verified_install_authority_for_test("0.1.0", COMMIT, 8).unwrap();
    assert!(
        store
            .enqueue_automation_execution_job(&request(), &reactivated, 100)
            .is_err()
    );

    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let first_claim = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 100)
        .unwrap();
    let mut predating = receipt_for(&first_claim);
    predating.started_at_unix_ms = 149;
    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-1",
                &predating,
                &candidate(),
                200,
            )
            .is_err()
    );
    store
        .record_automation_execution_attempt_failure(
            JOB_ID,
            "automation-worker-1",
            first_claim.claim_id.as_deref().unwrap(),
            "adapter_timeout",
            true,
            200,
        )
        .unwrap();
    store
        .claim_automation_execution_job(JOB_ID, "automation-worker-2", &candidate(), 60_200, 1_000)
        .unwrap();
    assert!(
        store
            .complete_automation_execution_job(
                JOB_ID,
                "automation-worker-2",
                &receipt_for(&first_claim),
                &candidate(),
                350,
            )
            .is_err()
    );
    assert!(
        store
            .renew_automation_execution_job_lease(
                JOB_ID,
                "automation-worker-2",
                first_claim.claim_id.as_deref().unwrap(),
                &reactivated,
                300,
                1_000,
            )
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_fifo_head_is_terminalized_before_a_current_job_is_claimed() {
    let (database, store) = configured_store("automation-execution-stale-fifo");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    store
        .write_automation_feature_policy(&policy(2), Some(1), "setup-admin", 150)
        .unwrap();
    let mut evidence = connection_evidence();
    evidence.evidence_id = "88888888-2222-4333-8444-555555555555".into();
    evidence.policy_revision = 2;
    evidence.tested_at_unix_ms = 160;
    store
        .append_automation_connection_test_evidence(&evidence, &candidate_identity(), 160)
        .unwrap();
    let mut current_request = request();
    current_request.job_id = "99999999-2222-4333-8444-555555555555".into();
    current_request.policy_revision = 2;
    current_request.requested_at_unix_ms = 200;
    store
        .enqueue_automation_execution_job(&current_request, &candidate(), 200)
        .unwrap();

    let claimed = store
        .claim_next_automation_execution_job("automation-worker-1", &candidate(), 250, 1_000)
        .unwrap()
        .unwrap();
    assert_eq!(claimed.request.job_id, current_request.job_id);
    let stale = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(stale.state, AutomationExecutionJobState::Failed);
    assert_eq!(
        stale.last_failure_code.as_deref(),
        Some("authority_changed")
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn missing_parent_receipt_and_corrupt_failure_code_fail_closed() {
    let (database, store) = configured_store("automation-execution-storage-corruption");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let claimed = store
        .claim_automation_execution_job(JOB_ID, "automation-worker-1", &candidate(), 150, 1_000)
        .unwrap();
    store
        .complete_automation_execution_job(
            JOB_ID,
            "automation-worker-1",
            &receipt_for(&claimed),
            &candidate(),
            350,
        )
        .unwrap();
    drop(store);

    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER automation_execution_jobs_no_delete;\
             DELETE FROM automation_execution_jobs;\
             CREATE TRIGGER automation_execution_jobs_no_delete \
               BEFORE DELETE ON automation_execution_jobs \
               BEGIN SELECT RAISE(ABORT, 'automation execution jobs are durable history'); END"
        ),
        SQLITE_OK
    );
    let reopened = StationStore::open(&database).unwrap();
    assert!(reopened.read_automation_execution_job(JOB_ID).is_err());
    drop(reopened);
    let _ = std::fs::remove_file(&database);

    let (database, store) = configured_store("automation-execution-failure-corruption");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    drop(store);
    assert_eq!(
        raw_sql_status(
            &database,
            "UPDATE automation_execution_jobs SET last_failure_code='SECRET value that is neither categorical nor safe'"
        ),
        SQLITE_OK
    );
    let reopened = StationStore::open(&database).unwrap();
    assert!(reopened.read_automation_execution_job(JOB_ID).is_err());
    drop(reopened);
    let _ = std::fs::remove_file(database);
}

fn exact_test_adapter() -> AutomationExecutionAdapter {
    let system_root = std::env::var_os("SystemRoot").expect("Windows system root exists");
    let application_root = PathBuf::from(&system_root);
    let executable = PathBuf::from(system_root)
        .join("System32")
        .join("WindowsPowerShell")
        .join("v1.0")
        .join("powershell.exe");
    let mut exact_receipt = receipt();
    exact_receipt.claim_id = "CLAIM_PLACEHOLDER".into();
    let payload = serde_json::to_string(&exact_receipt).expect("serialize exact receipt");
    let encoded_payload = STANDARD.encode(payload);
    let script = format!(
        "$payload=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_payload}'));[Console]::Out.Write($payload.Replace('CLAIM_PLACEHOLDER',$env:TOWNLIGHT_AUTOMATION_EXECUTION_CLAIM_ID))"
    );
    let encoded_script = STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    AutomationExecutionAdapter::new(
        ADAPTER_ID,
        1,
        application_root,
        executable,
        sha256_file(
            &PathBuf::from(std::env::var_os("SystemRoot").unwrap())
                .join("System32")
                .join("WindowsPowerShell")
                .join("v1.0")
                .join("powershell.exe"),
        ),
        vec![
            "-NoLogo".into(),
            "-NoProfile".into(),
            "-NonInteractive".into(),
            "-EncodedCommand".into(),
            encoded_script.into(),
        ],
    )
    .expect("build host-owned test worker")
}

fn blocking_test_adapter() -> AutomationExecutionAdapter {
    let system_root = std::env::var_os("SystemRoot").expect("Windows system root exists");
    let application_root = PathBuf::from(&system_root);
    let executable = PathBuf::from(&system_root)
        .join("System32")
        .join("WindowsPowerShell")
        .join("v1.0")
        .join("powershell.exe");
    AutomationExecutionAdapter::new(
        ADAPTER_ID,
        1,
        application_root,
        &executable,
        sha256_file(&executable),
        vec![
            "-NoLogo".into(),
            "-NoProfile".into(),
            "-NonInteractive".into(),
            "-Command".into(),
            "Start-Sleep -Seconds 30".into(),
        ],
    )
    .expect("build blocking host-owned test worker")
}

fn descendant_holding_pipe_test_adapter() -> AutomationExecutionAdapter {
    let system_root = std::env::var_os("SystemRoot").expect("Windows system root exists");
    let application_root = PathBuf::from(&system_root);
    let executable = PathBuf::from(&system_root)
        .join("System32")
        .join("WindowsPowerShell")
        .join("v1.0")
        .join("powershell.exe");
    // This worker starts a child before sleeping. The controller must contain
    // that descendant in the same Job Object and drain its inherited pipes
    // while stopping the parent; otherwise this test would hang at teardown.
    let script = "Start-Process -FilePath powershell.exe -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','[Console]::Out.Write(\"held\"); Start-Sleep -Seconds 30'; Start-Sleep -Seconds 30";
    AutomationExecutionAdapter::new(
        ADAPTER_ID,
        1,
        application_root,
        &executable,
        sha256_file(&executable),
        vec![
            "-NoLogo".into(),
            "-NoProfile".into(),
            "-NonInteractive".into(),
            "-Command".into(),
            script.into(),
        ],
    )
    .expect("build descendant worker")
}

fn sha256_file(path: &Path) -> String {
    let bytes = std::fs::read(path).expect("read test worker");
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn controller_commits_only_an_exact_typed_adapter_receipt() {
    let (database, store) = configured_store("automation-execution-controller-success");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let adapter = exact_test_adapter();
    let mut times = [150_i64, 151, 152, 350].into_iter();

    assert!(
        run_automation_execution_controller_iteration(
            &store,
            Some(&adapter),
            &candidate(),
            "stationd-test-controller",
            &mut || Ok(times.next().unwrap_or(350)),
        )
        .unwrap()
    );

    let completed = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(completed.state, AutomationExecutionJobState::Completed);
    assert_eq!(completed.attempt_count, 1);
    assert_eq!(
        completed
            .receipt
            .as_ref()
            .map(|receipt| receipt.claim_id.as_str()),
        completed.claim_id.as_deref()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn controller_without_an_implemented_exact_adapter_records_no_success() {
    let (database, store) = configured_store("automation-execution-controller-unavailable");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let mut times = [150_i64, 200].into_iter();

    assert!(
        run_automation_execution_controller_iteration(
            &store,
            None,
            &candidate(),
            "stationd-test-controller",
            &mut || Ok(times.next().expect("controller clock call is bounded")),
        )
        .unwrap()
    );

    let pending = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(pending.attempt_count, 1);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_unavailable")
    );
    assert!(pending.receipt.is_none());

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn host_owned_blocking_worker_is_terminated_on_stop_and_releases_its_claim_for_a_paced_retry() {
    let (database, store) = configured_store("automation-execution-stop-containment");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let adapter = blocking_test_adapter();
    let stop = AtomicBool::new(false);
    let mut calls = 0_u8;
    let mut times = [150_i64, 151, 152, 200].into_iter();

    assert!(
        run_automation_execution_controller_iteration_with_stop(
            &store,
            Some(&adapter),
            &candidate(),
            "stationd-test-controller",
            &mut || {
                calls += 1;
                if calls == 3 {
                    stop.store(true, Ordering::Release);
                }
                Ok(times.next().unwrap())
            },
            &stop,
        )
        .expect("host must stop its own process tree without waiting for worker cooperation")
    );
    let pending = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_timeout")
    );
    assert_eq!(pending.next_attempt_at_unix_ms, 60_200);
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn job_containment_reaps_an_early_descendant_holding_a_pipe() {
    let (database, store) = configured_store("automation-execution-descendant-pipe");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let adapter = descendant_holding_pipe_test_adapter();
    let stop = AtomicBool::new(false);
    let mut calls = 0_u8;
    let mut times = [150_i64, 151, 152, 200].into_iter();

    assert!(
        run_automation_execution_controller_iteration_with_stop(
            &store,
            Some(&adapter),
            &candidate(),
            "stationd-test-controller",
            &mut || {
                calls += 1;
                if calls == 3 {
                    stop.store(true, Ordering::Release);
                }
                Ok(times.next().unwrap())
            },
            &stop,
        )
        .expect("contained descendant and both drain threads must be reaped")
    );
    let pending = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_timeout")
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

fn assert_setup_fault_is_bounded_and_fails_closed(
    fault: AutomationExecutionSetupFault,
    name: &str,
) {
    let (database, store) = configured_store(name);
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let adapter = blocking_test_adapter().with_test_setup_fault(fault);
    let mut times = [150_i64, 151, 200].into_iter();
    let started = Instant::now();
    assert!(
        run_automation_execution_controller_iteration(
            &store,
            Some(&adapter),
            &candidate(),
            "stationd-test-controller",
            &mut || Ok(times.next().unwrap()),
        )
        .expect("setup fault must kill and bounded-reap its exact suspended child")
    );
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "setup fault held the fence beyond bounded reap"
    );
    let pending = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_unavailable")
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn forced_job_assignment_failure_kills_and_bounded_reaps_exact_suspended_child() {
    assert_setup_fault_is_bounded_and_fails_closed(
        AutomationExecutionSetupFault::AssignProcess,
        "automation-execution-forced-assign-failure",
    );
}

#[test]
fn forced_primary_thread_resume_failure_terminates_job_and_bounded_reaps_child() {
    assert_setup_fault_is_bounded_and_fails_closed(
        AutomationExecutionSetupFault::ResumeThread,
        "automation-execution-forced-resume-failure",
    );
}

#[test]
fn action_deadline_is_monotonic_across_lease_renewal_and_wall_clock_rollback() {
    let (database, store) = configured_store("automation-execution-deadline-containment");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let adapter = blocking_test_adapter();
    let stop = AtomicBool::new(false);
    // The third clock observation reaches the renewal lead, extending only
    // durable ownership. The fourth reaches the immutable action deadline.
    let mut times = [150_i64, 151, 30_150, 100, 101].into_iter();
    let mut elapsed = [
        Duration::ZERO,
        Duration::from_secs(30),
        Duration::from_secs(60),
    ]
    .into_iter();

    assert!(
        run_automation_execution_controller_iteration_with_stop_and_elapsed(
            &store,
            Some(&adapter),
            &candidate(),
            "stationd-test-controller",
            &mut || Ok(times.next().unwrap()),
            &stop,
            &mut || elapsed.next().unwrap(),
        )
        .expect("deadline teardown must not require a blocking worker callback")
    );
    let pending = store
        .read_automation_execution_job(JOB_ID)
        .expect("read terminated action")
        .expect("job remains durable");
    assert_eq!(pending.state, AutomationExecutionJobState::Pending);
    assert_eq!(pending.attempt_count, 1);
    assert_eq!(
        pending.last_failure_code.as_deref(),
        Some("adapter_timeout")
    );
    // Attempt one retains its normal durable backoff; a lease renewal cannot
    // turn the action into an unbounded run or a hot retry.
    assert_eq!(pending.next_attempt_at_unix_ms, 60_101);
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn unavailable_adapter_retries_are_durable_paced_and_survive_restart() {
    let (database, store) = configured_store("automation-execution-unavailable-backoff");
    store
        .enqueue_automation_execution_job(&request(), &candidate(), 100)
        .unwrap();
    let mut first_times = [150_i64, 200].into_iter();
    assert!(
        run_automation_execution_controller_iteration(
            &store,
            None,
            &candidate(),
            "stationd-test-controller",
            &mut || Ok(first_times.next().unwrap()),
        )
        .unwrap()
    );
    let first = store
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(first.attempt_count, 1);
    assert_eq!(first.next_attempt_at_unix_ms, 60_200);
    assert!(
        store
            .claim_next_automation_execution_job("other-controller", &candidate(), 60_199, 1_000)
            .unwrap()
            .is_none()
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let second = reopened
        .claim_next_automation_execution_job("other-controller", &candidate(), 60_200, 1_000)
        .unwrap()
        .unwrap();
    assert_eq!(second.attempt_count, 2);
    reopened
        .record_automation_execution_attempt_failure(
            JOB_ID,
            "other-controller",
            second.claim_id.as_deref().unwrap(),
            "adapter_unavailable",
            true,
            60_250,
        )
        .unwrap();
    let second_pending = reopened
        .read_automation_execution_job(JOB_ID)
        .unwrap()
        .unwrap();
    assert_eq!(second_pending.next_attempt_at_unix_ms, 180_250);
    drop(reopened);
    let _ = std::fs::remove_file(database);
}
