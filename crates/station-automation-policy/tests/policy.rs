use station_automation_policy::{
    AdapterLocation, AdaptiveDefaultHint, AutomationAdapterDescriptor, AutomationCandidateIdentity,
    AutomationConnectionTestEvidence, AutomationFeature, AutomationFeaturePolicy,
    AutomationReadinessStatus, ConnectionTestOutcome, CredentialAvailability,
    FeatureAutomationStatus, derive_automation_readiness, derive_feature_automation_status,
};

const CANDIDATE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CONFIGURATION: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn candidate() -> AutomationCandidateIdentity {
    AutomationCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: CANDIDATE.into(),
    }
}

fn policy() -> AutomationFeaturePolicy {
    AutomationFeaturePolicy {
        policy_id: "11111111-2222-4333-8444-555555555555".into(),
        feature: AutomationFeature::Summaries,
        operator_selected_adapter_id: Some("22222222-2222-4333-8444-555555555555".into()),
        adaptive_default: None,
        configuration_hash: CONFIGURATION.into(),
        revision: 3,
    }
}

fn adapter() -> AutomationAdapterDescriptor {
    AutomationAdapterDescriptor {
        adapter_id: "22222222-2222-4333-8444-555555555555".into(),
        location: AdapterLocation::Local,
        model_key: "gemma4-12b-ollama".into(),
        model_identifier: "gemma4:12b".into(),
        supported_features: vec![AutomationFeature::Summaries],
        credential_availability: CredentialAvailability::NotRequired,
        revision: 5,
    }
}

fn evidence() -> AutomationConnectionTestEvidence {
    AutomationConnectionTestEvidence {
        evidence_id: "33333333-2222-4333-8444-555555555555".into(),
        policy_id: policy().policy_id,
        adapter_id: adapter().adapter_id,
        policy_revision: 3,
        adapter_revision: 5,
        configuration_hash: CONFIGURATION.into(),
        candidate: candidate(),
        outcome: ConnectionTestOutcome::Passed,
        tested_at_unix_ms: 100,
        expires_at_unix_ms: 10_000,
        detail: "Connection test passed.".into(),
    }
}

#[test]
fn only_current_operator_selected_capability_with_passing_evidence_is_green() {
    assert_eq!(
        derive_feature_automation_status(
            &policy(),
            &[adapter()],
            Some(&evidence()),
            &candidate(),
            200
        ),
        FeatureAutomationStatus::Passed
    );
}

#[test]
fn cloud_capability_requires_an_explicit_credential_availability_fact() {
    let mut cloud_without_credential_fact = adapter();
    cloud_without_credential_fact.location = AdapterLocation::Cloud;
    assert_eq!(
        derive_feature_automation_status(
            &policy(),
            &[cloud_without_credential_fact],
            Some(&evidence()),
            &candidate(),
            200,
        ),
        FeatureAutomationStatus::InvalidConfiguration
    );
}

#[test]
fn adaptive_default_is_advisory_and_never_replaces_operator_selection() {
    let mut policy = policy();
    policy.operator_selected_adapter_id = None;
    policy.adaptive_default = Some(AdaptiveDefaultHint {
        minimum_memory_gb: 16,
        preferred_adapter_id: adapter().adapter_id,
        fallback_adapter_id: "44444444-2222-4333-8444-555555555555".into(),
    });
    assert_eq!(
        derive_feature_automation_status(
            &policy,
            &[adapter()],
            Some(&evidence()),
            &candidate(),
            200
        ),
        FeatureAutomationStatus::SelectionMissing
    );
}

#[test]
fn future_stale_and_expired_evidence_fails_closed() {
    let mut future = evidence();
    future.tested_at_unix_ms = 201;
    assert_eq!(
        derive_feature_automation_status(&policy(), &[adapter()], Some(&future), &candidate(), 200),
        FeatureAutomationStatus::InvalidEvidence
    );

    let mut stale_configuration = evidence();
    stale_configuration.configuration_hash =
        "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc".into();
    assert_eq!(
        derive_feature_automation_status(
            &policy(),
            &[adapter()],
            Some(&stale_configuration),
            &candidate(),
            200,
        ),
        FeatureAutomationStatus::StaleConfiguration
    );

    let mut stale_candidate = evidence();
    stale_candidate.candidate.source_commit = "cccccccccccccccccccccccccccccccccccccccc".into();
    assert_eq!(
        derive_feature_automation_status(
            &policy(),
            &[adapter()],
            Some(&stale_candidate),
            &candidate(),
            200,
        ),
        FeatureAutomationStatus::StaleCandidate
    );

    assert_eq!(
        derive_feature_automation_status(
            &policy(),
            &[adapter()],
            Some(&evidence()),
            &candidate(),
            10_000
        ),
        FeatureAutomationStatus::Expired
    );
}

#[test]
fn duplicate_or_malformed_authority_records_are_rejected() {
    assert!(
        derive_automation_readiness(&[adapter()], &[policy(), policy()], &[], &candidate(), 200,)
            .is_err()
    );

    let mut reused_evidence_id = evidence();
    reused_evidence_id.policy_id = "55555555-2222-4333-8444-555555555555".into();
    assert!(
        derive_automation_readiness(
            &[],
            &[],
            &[evidence(), reused_evidence_id],
            &candidate(),
            200,
        )
        .is_err()
    );

    let mut malformed = adapter();
    malformed.adapter_id = "22222222-2222-4333-8444-55555555555Z".into();
    assert!(malformed.validate().is_err());
}

#[test]
fn missing_feature_policies_block_overall_readiness() {
    let readiness =
        derive_automation_readiness(&[adapter()], &[policy()], &[evidence()], &candidate(), 200)
            .unwrap();
    assert_eq!(readiness.summaries, FeatureAutomationStatus::Passed);
    assert_eq!(
        readiness.captions,
        FeatureAutomationStatus::SelectionMissing
    );
    assert_eq!(
        readiness.translation,
        FeatureAutomationStatus::SelectionMissing
    );
    assert_eq!(readiness.overall, AutomationReadinessStatus::Blocked);
}

#[test]
fn adapter_revision_must_be_nonzero() {
    let mut adapter = adapter();
    adapter.revision = 0;
    assert!(adapter.validate().is_err());
}

#[test]
fn adaptive_hints_must_name_valid_feature_capabilities_without_selecting_them() {
    let mut dangling_hint = policy();
    dangling_hint.adaptive_default = Some(AdaptiveDefaultHint {
        minimum_memory_gb: 16,
        preferred_adapter_id: adapter().adapter_id,
        fallback_adapter_id: "44444444-2222-4333-8444-555555555555".into(),
    });
    assert_eq!(
        derive_feature_automation_status(
            &dangling_hint,
            &[adapter()],
            Some(&evidence()),
            &candidate(),
            200,
        ),
        FeatureAutomationStatus::InvalidConfiguration
    );

    let mut unsupported_adapter = adapter();
    unsupported_adapter.adapter_id = "44444444-2222-4333-8444-555555555555".into();
    unsupported_adapter.supported_features = vec![AutomationFeature::Captions];
    unsupported_adapter.revision = 6;
    let mut unsupported_hint = policy();
    unsupported_hint.adaptive_default = Some(AdaptiveDefaultHint {
        minimum_memory_gb: 16,
        preferred_adapter_id: adapter().adapter_id,
        fallback_adapter_id: unsupported_adapter.adapter_id.clone(),
    });
    assert_eq!(
        derive_feature_automation_status(
            &unsupported_hint,
            &[adapter(), unsupported_adapter],
            Some(&evidence()),
            &candidate(),
            200,
        ),
        FeatureAutomationStatus::InvalidConfiguration
    );
}
