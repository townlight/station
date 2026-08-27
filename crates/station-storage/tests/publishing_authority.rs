use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_publishing::{
    ConnectionTestOutcome, PublishCandidateIdentity, PublishConnectionTestEvidence,
    PublishTargetAccount, PublishTargetKind, PublishTargetProfile, RequiredTierStatus,
};
use station_storage::{PublishingAuthorityWriteError, StationStore};

const ACCOUNT_ID: &str = "11111111-2222-4333-8444-555555555555";
const TARGET_ID: &str = "22222222-2222-4333-8444-555555555555";
const CANDIDATE_COMMIT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CONFIGURATION_HASH: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn temporary_database() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-publishing-authority-{nonce}.db"))
}

fn candidate() -> PublishCandidateIdentity {
    PublishCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: CANDIDATE_COMMIT.into(),
    }
}

fn account(revision: u64) -> PublishTargetAccount {
    PublishTargetAccount {
        account_id: ACCOUNT_ID.into(),
        kind: PublishTargetKind::Portal,
        label: "Portal account".into(),
        enabled: true,
        credential_configured: true,
        revision,
    }
}

fn target(revision: u64) -> PublishTargetProfile {
    PublishTargetProfile {
        target_id: TARGET_ID.into(),
        account_id: ACCOUNT_ID.into(),
        kind: PublishTargetKind::Portal,
        label: "Portal target".into(),
        enabled: true,
        configuration_hash: CONFIGURATION_HASH.into(),
        revision,
    }
}

fn evidence() -> PublishConnectionTestEvidence {
    PublishConnectionTestEvidence {
        evidence_id: "33333333-2222-4333-8444-555555555555".into(),
        target_id: TARGET_ID.into(),
        account_revision: 1,
        target_revision: 1,
        configuration_hash: CONFIGURATION_HASH.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 100,
        expires_at_unix_ms: 10_000,
        detail: "Trusted provider connection test passed.".into(),
    }
}

#[test]
fn publishing_authority_persists_cas_history_and_current_candidate_evidence() {
    let database = temporary_database();
    let store = StationStore::open(&database).unwrap();
    store
        .write_publish_target_account(&account(1), None, "setup-admin", 10)
        .unwrap();
    store
        .provision_publish_account_credential(
            ACCOUNT_ID,
            1,
            b"test-provider-secret",
            "setup-admin",
            10,
        )
        .unwrap();
    store
        .write_publish_target_profile(&target(1), None, "setup-admin", 11)
        .unwrap();
    store
        .append_publish_connection_test_evidence(&evidence(), &candidate(), 200)
        .unwrap();

    assert_eq!(
        store.read_publish_target_account(ACCOUNT_ID).unwrap(),
        Some(account(1))
    );
    assert_eq!(
        store.read_publish_target_profile(TARGET_ID).unwrap(),
        Some(target(1))
    );
    assert_eq!(
        store
            .list_publish_target_account_history(ACCOUNT_ID, 1)
            .unwrap()[0]
            .changed_by,
        "setup-admin"
    );
    assert_eq!(
        store
            .list_publish_target_profile_history(TARGET_ID, 1)
            .unwrap()[0]
            .occurred_at_unix_ms,
        11
    );
    assert_eq!(
        store
            .list_publish_connection_test_evidence(TARGET_ID, 1)
            .unwrap(),
        vec![evidence()]
    );
    let readiness = store
        .derive_current_publication_readiness(&candidate(), 200)
        .unwrap();
    assert_eq!(readiness.canonical_portal, RequiredTierStatus::Passed);
    assert_eq!(readiness.permanence, RequiredTierStatus::Blocked);
    assert_eq!(readiness.reach_capacity, RequiredTierStatus::Blocked);

    let mut next = account(2);
    next.label = "Renamed portal account".into();
    assert!(matches!(
        store.write_publish_target_account(&next, None, "setup-admin", 12),
        Err(PublishingAuthorityWriteError::Conflict {
            current_revision: Some(1)
        })
    ));
    store
        .write_publish_target_account(&next, Some(1), "setup-admin", 12)
        .unwrap();
    assert_eq!(
        store
            .list_publish_target_account_history(ACCOUNT_ID, 2)
            .unwrap()
            .len(),
        2
    );

    let mut stale = evidence();
    stale.evidence_id = "44444444-2222-4333-8444-555555555555".into();
    assert!(
        store
            .append_publish_connection_test_evidence(&stale, &candidate(), 200)
            .unwrap_err()
            .contains("not current")
    );

    drop(store);
    std::fs::remove_file(database).unwrap();
}
