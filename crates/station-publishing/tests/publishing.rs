use station_publishing::{
    ConnectionTestOutcome, PublishCandidateIdentity, PublishConnectionTestEvidence,
    PublishExecutionReceipt, PublishExecutionStage, PublishTargetAccount, PublishTargetKind,
    PublishTargetProfile, PublishTargetStatus, RequiredTierStatus, derive_publication_readiness,
    derive_publish_target_status, validate_receipt_successor,
};

const ACCOUNT_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const TARGET_ID: &str = "11111111-2222-4333-8444-555555555555";
const EVIDENCE_ID: &str = "22222222-3333-4444-8555-666666666666";
const CANDIDATE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CONFIGURATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn candidate() -> PublishCandidateIdentity {
    PublishCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: CANDIDATE.into(),
    }
}

fn portal_account() -> PublishTargetAccount {
    PublishTargetAccount {
        account_id: ACCOUNT_ID.into(),
        kind: PublishTargetKind::Portal,
        label: "Station portal account".into(),
        enabled: true,
        credential_configured: true,
        revision: 3,
    }
}

fn portal_target() -> PublishTargetProfile {
    PublishTargetProfile {
        target_id: TARGET_ID.into(),
        account_id: ACCOUNT_ID.into(),
        kind: PublishTargetKind::Portal,
        label: "Canonical portal".into(),
        enabled: true,
        configuration_hash: CONFIGURATION.into(),
        revision: 5,
    }
}

fn current_evidence() -> PublishConnectionTestEvidence {
    PublishConnectionTestEvidence {
        evidence_id: EVIDENCE_ID.into(),
        target_id: TARGET_ID.into(),
        account_revision: 3,
        target_revision: 5,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 100,
        expires_at_unix_ms: 10_000,
        detail: "Provider connection test passed.".into(),
    }
}

#[test]
fn current_passing_evidence_is_the_only_green_target_status() {
    assert_eq!(
        derive_publish_target_status(
            &portal_account(),
            &portal_target(),
            Some(&current_evidence()),
            &candidate(),
            200,
        ),
        PublishTargetStatus::Passed
    );
}

#[test]
fn changed_configuration_candidate_or_expired_evidence_fails_closed() {
    let mut changed_configuration = current_evidence();
    changed_configuration.configuration_hash =
        "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc".into();
    assert_eq!(
        derive_publish_target_status(
            &portal_account(),
            &portal_target(),
            Some(&changed_configuration),
            &candidate(),
            200,
        ),
        PublishTargetStatus::StaleConfiguration
    );

    let mut changed_candidate = current_evidence();
    changed_candidate.candidate.source_commit = "cccccccccccccccccccccccccccccccccccccccc".into();
    assert_eq!(
        derive_publish_target_status(
            &portal_account(),
            &portal_target(),
            Some(&changed_candidate),
            &candidate(),
            200,
        ),
        PublishTargetStatus::StaleCandidate
    );

    assert_eq!(
        derive_publish_target_status(
            &portal_account(),
            &portal_target(),
            Some(&current_evidence()),
            &candidate(),
            10_000,
        ),
        PublishTargetStatus::Expired
    );
}

#[test]
fn removed_credentials_cannot_leave_a_target_green() {
    let mut account = portal_account();
    account.credential_configured = false;
    assert_eq!(
        derive_publish_target_status(
            &account,
            &portal_target(),
            Some(&current_evidence()),
            &candidate(),
            200,
        ),
        PublishTargetStatus::CredentialUnavailable
    );
}

#[test]
fn future_dated_evidence_is_invalid_even_when_every_other_binding_matches() {
    let mut future_evidence = current_evidence();
    future_evidence.tested_at_unix_ms = 201;
    assert_eq!(
        derive_publish_target_status(
            &portal_account(),
            &portal_target(),
            Some(&future_evidence),
            &candidate(),
            200,
        ),
        PublishTargetStatus::InvalidEvidence
    );
}

fn account(account_id: &str, kind: PublishTargetKind) -> PublishTargetAccount {
    PublishTargetAccount {
        account_id: account_id.into(),
        kind,
        label: format!("{kind:?} account"),
        enabled: true,
        credential_configured: true,
        revision: 1,
    }
}

fn target(target_id: &str, account_id: &str, kind: PublishTargetKind) -> PublishTargetProfile {
    PublishTargetProfile {
        target_id: target_id.into(),
        account_id: account_id.into(),
        kind,
        label: format!("{kind:?} target"),
        configuration_hash: CONFIGURATION.into(),
        enabled: true,
        revision: 1,
    }
}

fn evidence(target_id: &str, kind: PublishTargetKind) -> PublishConnectionTestEvidence {
    PublishConnectionTestEvidence {
        evidence_id: match kind {
            PublishTargetKind::Portal => "aaaaaaaa-2222-4333-8444-555555555555",
            PublishTargetKind::InternetArchive => "bbbbbbbb-2222-4333-8444-555555555555",
            PublishTargetKind::LocalNas => "cccccccc-2222-4333-8444-555555555555",
            PublishTargetKind::YouTubeLive => "dddddddd-2222-4333-8444-555555555555",
            PublishTargetKind::Syndication => "eeeeeeee-2222-4333-8444-555555555555",
            PublishTargetKind::Podcast => "ffffffff-2222-4333-8444-555555555555",
        }
        .into(),
        target_id: target_id.into(),
        account_revision: 1,
        target_revision: 1,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 100,
        expires_at_unix_ms: 10_000,
        detail: format!("{kind:?} connection test passed."),
    }
}

#[test]
fn required_tiers_need_portal_archive_nas_and_youtube_while_optional_targets_do_not_substitute() {
    let accounts = vec![
        account(
            "11111111-2222-4333-8444-555555555551",
            PublishTargetKind::Portal,
        ),
        account(
            "11111111-2222-4333-8444-555555555552",
            PublishTargetKind::InternetArchive,
        ),
        account(
            "11111111-2222-4333-8444-555555555553",
            PublishTargetKind::LocalNas,
        ),
        account(
            "11111111-2222-4333-8444-555555555554",
            PublishTargetKind::YouTubeLive,
        ),
        account(
            "11111111-2222-4333-8444-555555555555",
            PublishTargetKind::Syndication,
        ),
    ];
    let targets = vec![
        target(
            "22222222-2222-4333-8444-555555555551",
            "11111111-2222-4333-8444-555555555551",
            PublishTargetKind::Portal,
        ),
        target(
            "22222222-2222-4333-8444-555555555552",
            "11111111-2222-4333-8444-555555555552",
            PublishTargetKind::InternetArchive,
        ),
        target(
            "22222222-2222-4333-8444-555555555553",
            "11111111-2222-4333-8444-555555555553",
            PublishTargetKind::LocalNas,
        ),
        target(
            "22222222-2222-4333-8444-555555555554",
            "11111111-2222-4333-8444-555555555554",
            PublishTargetKind::YouTubeLive,
        ),
        target(
            "22222222-2222-4333-8444-555555555555",
            "11111111-2222-4333-8444-555555555555",
            PublishTargetKind::Syndication,
        ),
    ];
    let evidence_records = vec![
        evidence(&targets[0].target_id, PublishTargetKind::Portal),
        evidence(&targets[1].target_id, PublishTargetKind::InternetArchive),
        evidence(&targets[2].target_id, PublishTargetKind::LocalNas),
        evidence(&targets[3].target_id, PublishTargetKind::YouTubeLive),
    ];
    let readiness =
        derive_publication_readiness(&accounts, &targets, &evidence_records, &candidate(), 200)
            .unwrap();
    assert_eq!(readiness.canonical_portal, RequiredTierStatus::Passed);
    assert_eq!(readiness.permanence, RequiredTierStatus::Passed);
    assert_eq!(readiness.reach_capacity, RequiredTierStatus::Passed);
    assert_eq!(readiness.targets[4].status, PublishTargetStatus::NotRun);

    let without_youtube = derive_publication_readiness(
        &accounts,
        &[
            targets[0].clone(),
            targets[1].clone(),
            targets[2].clone(),
            targets[4].clone(),
        ],
        &[
            evidence(&targets[0].target_id, PublishTargetKind::Portal),
            evidence(&targets[1].target_id, PublishTargetKind::InternetArchive),
            evidence(&targets[2].target_id, PublishTargetKind::LocalNas),
            evidence(&targets[4].target_id, PublishTargetKind::Syndication),
        ],
        &candidate(),
        200,
    )
    .unwrap();
    assert_eq!(without_youtube.reach_capacity, RequiredTierStatus::Blocked);
}

#[test]
fn malformed_or_duplicate_evidence_is_rejected_before_tier_derivation() {
    let account = portal_account();
    let target = portal_target();
    let duplicate = current_evidence();
    assert!(
        derive_publication_readiness(
            &[account],
            &[target],
            &[duplicate.clone(), duplicate],
            &candidate(),
            200,
        )
        .is_err()
    );

    let mut reused_evidence_id = current_evidence();
    reused_evidence_id.target_id = "33333333-2222-4333-8444-555555555555".into();
    assert!(
        derive_publication_readiness(
            &[],
            &[],
            &[current_evidence(), reused_evidence_id],
            &candidate(),
            200,
        )
        .is_err()
    );

    let mut malformed = portal_target();
    malformed.configuration_hash = "not-a-hash".into();
    assert!(malformed.validate().is_err());
}

#[test]
fn uppercase_uuid_is_not_a_canonical_identity() {
    let mut uppercase_identity = portal_account();
    uppercase_identity.account_id = "8B626C01-BDF8-419A-8A2E-B0A7CAA1FF7E".into();
    assert!(uppercase_identity.validate().is_err());
}

fn receipt(kind: PublishTargetKind, stage: PublishExecutionStage) -> PublishExecutionReceipt {
    PublishExecutionReceipt {
        receipt_id: "44444444-2222-4333-8444-555555555555".into(),
        job_id: "55555555-2222-4333-8444-555555555555".into(),
        account_id: ACCOUNT_ID.into(),
        account_revision: 3,
        target_id: TARGET_ID.into(),
        target_revision: 5,
        target_kind: kind,
        configuration_hash: CONFIGURATION.into(),
        credential_version: 2,
        candidate: candidate(),
        media_sha256: "d".repeat(64),
        media_bytes: 188,
        stage,
        delivery_identity_sha256: "e".repeat(64),
        occurred_at_unix_ms: 100,
        recorded_at_unix_ms: 101,
        detail: "Provider acknowledged the exact bounded request.".into(),
    }
}

#[test]
fn execution_truth_does_not_overclaim_provider_success() {
    assert!(
        !receipt(PublishTargetKind::Portal, PublishExecutionStage::Uploaded)
            .satisfies_required_delivery()
    );
    assert!(
        receipt(PublishTargetKind::Portal, PublishExecutionStage::Published)
            .satisfies_required_delivery()
    );
    assert!(
        receipt(
            PublishTargetKind::LocalNas,
            PublishExecutionStage::Delivered
        )
        .satisfies_required_delivery()
    );
    assert!(
        !receipt(
            PublishTargetKind::YouTubeLive,
            PublishExecutionStage::Delivered
        )
        .satisfies_required_delivery()
    );
    assert!(
        !receipt(
            PublishTargetKind::Podcast,
            PublishExecutionStage::ExternallyVerified
        )
        .satisfies_required_delivery()
    );
}

#[test]
fn execution_receipts_advance_only_the_same_exact_binding() {
    let prior = receipt(PublishTargetKind::Portal, PublishExecutionStage::Attempted);
    let mut next = prior.clone();
    next.receipt_id = "66666666-2222-4333-8444-555555555555".into();
    next.stage = PublishExecutionStage::Published;
    next.occurred_at_unix_ms = 102;
    next.recorded_at_unix_ms = 102;
    assert!(validate_receipt_successor(&prior, &next).is_ok());

    next.target_revision += 1;
    assert!(validate_receipt_successor(&prior, &next).is_err());
}
