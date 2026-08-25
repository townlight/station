use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_automation_policy::{
    AdapterLocation, AutomationAdapterDescriptor, AutomationCandidateIdentity,
    AutomationConnectionTestEvidence, AutomationFeature, AutomationFeaturePolicy,
    ConnectionTestOutcome, CredentialAvailability,
};
use station_storage::StationStore;

const ADAPTER_ID: &str = "11111111-2222-4333-8444-555555555555";
const POLICY_ID: &str = "22222222-2222-4333-8444-555555555555";
const EVIDENCE_ID: &str = "33333333-2222-4333-8444-555555555555";
const CONFIGURATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
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
        model_key: "gemma4-12b-ollama".into(),
        model_identifier: "gemma4:12b".into(),
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

fn evidence(evidence_id: &str) -> AutomationConnectionTestEvidence {
    AutomationConnectionTestEvidence {
        evidence_id: evidence_id.into(),
        policy_id: POLICY_ID.into(),
        adapter_id: ADAPTER_ID.into(),
        policy_revision: 1,
        adapter_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 100,
        expires_at_unix_ms: 10_000,
        detail: "Connection test passed.".into(),
    }
}

#[test]
fn revisioned_policy_and_current_candidate_evidence_round_trip_as_append_only_authority() {
    let database = temporary_database("automation-policy-round-trip");
    let store = StationStore::open(&database).unwrap();
    store
        .write_automation_adapter_descriptor(&adapter(1), None, "setup-admin", 10)
        .unwrap();
    store
        .write_automation_feature_policy(&policy(1), None, "setup-admin", 20)
        .unwrap();
    let test_evidence = evidence(EVIDENCE_ID);
    store
        .append_automation_connection_test_evidence(&test_evidence, &candidate(), 200)
        .unwrap();

    assert_eq!(
        store
            .read_automation_adapter_descriptor(ADAPTER_ID)
            .unwrap(),
        Some(adapter(1))
    );
    assert_eq!(
        store.read_automation_feature_policy(POLICY_ID).unwrap(),
        Some(policy(1))
    );
    assert_eq!(
        store
            .list_automation_connection_test_evidence(POLICY_ID, 10)
            .unwrap(),
        vec![test_evidence.clone()]
    );
    assert!(
        store
            .append_automation_connection_test_evidence(&test_evidence, &candidate(), 200)
            .is_err()
    );

    let stale_candidate = AutomationCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: "cccccccccccccccccccccccccccccccccccccccc".into(),
    };
    assert!(
        store
            .append_automation_connection_test_evidence(
                &evidence("44444444-2222-4333-8444-555555555555"),
                &stale_candidate,
                200,
            )
            .is_err()
    );

    assert!(
        store
            .write_automation_adapter_descriptor(&adapter(2), None, "setup-admin", 30)
            .is_err()
    );
    store
        .write_automation_adapter_descriptor(&adapter(2), Some(1), "setup-admin", 30)
        .unwrap();
    assert_eq!(
        store
            .list_automation_adapter_descriptor_history(ADAPTER_ID, 128)
            .unwrap()
            .len(),
        2
    );
    assert!(
        store
            .list_automation_adapter_descriptor_history(ADAPTER_ID, 0)
            .is_err()
    );

    assert!(
        store
            .write_automation_feature_policy(&policy(2), None, "setup-admin", 40)
            .is_err()
    );
    store
        .write_automation_feature_policy(&policy(2), Some(1), "setup-admin", 40)
        .unwrap();
    assert_eq!(
        store.read_automation_feature_policy(POLICY_ID).unwrap(),
        Some(policy(2))
    );
    assert_eq!(
        store
            .list_automation_feature_policy_history(POLICY_ID, 128)
            .unwrap()
            .len(),
        2
    );
    assert!(
        store
            .list_automation_feature_policy_history(POLICY_ID, 0)
            .is_err()
    );

    let mut rebound = policy(3);
    rebound.feature = AutomationFeature::Captions;
    assert!(
        store
            .write_automation_feature_policy(&rebound, Some(2), "setup-admin", 50)
            .is_err()
    );

    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn unsupported_policy_or_hint_and_stale_or_future_evidence_are_rejected() {
    let database = temporary_database("automation-policy-rejections");
    let store = StationStore::open(&database).unwrap();
    store
        .write_automation_adapter_descriptor(&adapter(1), None, "setup-admin", 10)
        .unwrap();

    let mut unsupported = policy(1);
    unsupported.feature = AutomationFeature::Captions;
    assert!(
        store
            .write_automation_feature_policy(&unsupported, None, "setup-admin", 20)
            .is_err()
    );

    let mut dangling_hint = policy(1);
    dangling_hint.adaptive_default = Some(station_automation_policy::AdaptiveDefaultHint {
        minimum_memory_gb: 16,
        preferred_adapter_id: ADAPTER_ID.into(),
        fallback_adapter_id: "44444444-2222-4333-8444-555555555555".into(),
    });
    assert!(
        store
            .write_automation_feature_policy(&dangling_hint, None, "setup-admin", 20)
            .is_err()
    );

    store
        .write_automation_feature_policy(&policy(1), None, "setup-admin", 20)
        .unwrap();
    let mut duplicate_feature = policy(1);
    duplicate_feature.policy_id = "55555555-2222-4333-8444-555555555555".into();
    assert!(
        store
            .write_automation_feature_policy(&duplicate_feature, None, "setup-admin", 21)
            .is_err()
    );
    let mut future = evidence(EVIDENCE_ID);
    future.tested_at_unix_ms = 201;
    assert!(
        store
            .append_automation_connection_test_evidence(&future, &candidate(), 200)
            .is_err()
    );
    let mut stale_revision = evidence(EVIDENCE_ID);
    stale_revision.adapter_revision = 2;
    assert!(
        store
            .append_automation_connection_test_evidence(&stale_revision, &candidate(), 200)
            .is_err()
    );
    let mut expired = evidence(EVIDENCE_ID);
    expired.expires_at_unix_ms = 200;
    assert!(
        store
            .append_automation_connection_test_evidence(&expired, &candidate(), 200)
            .is_err()
    );

    drop(store);
    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.read_automation_feature_policy(POLICY_ID).unwrap(),
        Some(policy(1))
    );
    drop(reopened);
    let _ = std::fs::remove_file(database);
}
