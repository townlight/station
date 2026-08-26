use station_automation_policy::{
    AdapterLocation, AutomationAdapterDescriptor, AutomationCandidateIdentity,
    AutomationExecutionEngine, AutomationExecutionReceipt, AutomationExecutionRequest,
    AutomationFeature, AutomationFeaturePolicy, AutomationInputArtifact, AutomationInputKind,
    AutomationOutputArtifact, AutomationOutputFacts, CredentialAvailability,
};

const INPUT_SHA: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const OUTPUT_SHA: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const CONFIG_SHA: &str = "3333333333333333333333333333333333333333333333333333333333333333";
const RUNTIME_SHA: &str = "4444444444444444444444444444444444444444444444444444444444444444";
const MODEL_SHA: &str = "5555555555555555555555555555555555555555555555555555555555555555";
const SOURCE_COMMIT: &str = "6666666666666666666666666666666666666666";

fn candidate() -> AutomationCandidateIdentity {
    AutomationCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: SOURCE_COMMIT.into(),
    }
}

fn policy(feature: AutomationFeature) -> AutomationFeaturePolicy {
    AutomationFeaturePolicy {
        policy_id: "11111111-2222-4333-8444-555555555555".into(),
        feature,
        operator_selected_adapter_id: Some("22222222-2222-4333-8444-555555555555".into()),
        adaptive_default: None,
        configuration_hash: CONFIG_SHA.into(),
        revision: 3,
    }
}

fn adapter(feature: AutomationFeature, location: AdapterLocation) -> AutomationAdapterDescriptor {
    AutomationAdapterDescriptor {
        adapter_id: "22222222-2222-4333-8444-555555555555".into(),
        location,
        model_key: "townlight-summary-default".into(),
        model_identifier: "provider/model-v1".into(),
        supported_features: vec![feature],
        credential_availability: match location {
            AdapterLocation::Local => CredentialAvailability::NotRequired,
            AdapterLocation::Cloud => CredentialAvailability::Available,
        },
        revision: 5,
    }
}

fn request(feature: AutomationFeature) -> AutomationExecutionRequest {
    AutomationExecutionRequest {
        job_id: "33333333-2222-4333-8444-555555555555".into(),
        feature,
        policy_id: policy(feature).policy_id,
        policy_revision: 3,
        adapter_id: adapter(feature, AdapterLocation::Local).adapter_id,
        adapter_revision: 5,
        configuration_hash: CONFIG_SHA.into(),
        candidate: candidate(),
        input: AutomationInputArtifact {
            kind: AutomationInputKind::Transcript,
            artifact_sha256: INPUT_SHA.into(),
            bytes: 4_096,
            media_type: "text/plain; charset=utf-8".into(),
            language: Some("en-US".into()),
        },
        requested_output_language: None,
        requested_at_unix_ms: 1_000,
    }
}

fn summary_receipt() -> AutomationExecutionReceipt {
    AutomationExecutionReceipt {
        receipt_id: "44444444-2222-4333-8444-555555555555".into(),
        job_id: "33333333-2222-4333-8444-555555555555".into(),
        feature: AutomationFeature::Summaries,
        policy_id: "11111111-2222-4333-8444-555555555555".into(),
        policy_revision: 3,
        adapter_id: "22222222-2222-4333-8444-555555555555".into(),
        adapter_revision: 5,
        configuration_hash: CONFIG_SHA.into(),
        candidate: candidate(),
        engine: AutomationExecutionEngine::Local {
            executable_sha256: RUNTIME_SHA.into(),
            model_artifact_sha256: MODEL_SHA.into(),
        },
        input: request(AutomationFeature::Summaries).input,
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
        started_at_unix_ms: 1_100,
        completed_at_unix_ms: 2_000,
    }
}

#[test]
fn exact_local_summary_execution_receipt_matches_current_authority() {
    let request = request(AutomationFeature::Summaries);
    summary_receipt()
        .validate_against(
            &request,
            &policy(AutomationFeature::Summaries),
            &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
            &candidate(),
        )
        .unwrap();
}

#[test]
fn receipt_rejects_cross_feature_or_stale_binding() {
    let request = request(AutomationFeature::Summaries);
    let mut receipt = summary_receipt();
    receipt.facts = AutomationOutputFacts::Captions {
        language: "en-US".into(),
        cue_count: 12,
        text_sha256: OUTPUT_SHA.into(),
        first_offset_ms: 0,
        last_end_ms: 60_000,
    };
    assert!(
        receipt
            .validate_against(
                &request,
                &policy(AutomationFeature::Summaries),
                &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );

    let mut stale = summary_receipt();
    stale.configuration_hash =
        "7777777777777777777777777777777777777777777777777777777777777777".into();
    assert!(
        stale
            .validate_against(
                &request,
                &policy(AutomationFeature::Summaries),
                &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );
}

#[test]
fn local_and_cloud_engine_proof_cannot_be_interchanged() {
    let request = request(AutomationFeature::Summaries);
    let mut cloud_receipt = summary_receipt();
    cloud_receipt.engine = AutomationExecutionEngine::Cloud {
        provider_response_sha256: RUNTIME_SHA.into(),
        provider_request_id_sha256: MODEL_SHA.into(),
    };
    assert!(
        cloud_receipt
            .validate_against(
                &request,
                &policy(AutomationFeature::Summaries),
                &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );

    cloud_receipt
        .validate_against(
            &request,
            &policy(AutomationFeature::Summaries),
            &adapter(AutomationFeature::Summaries, AdapterLocation::Cloud),
            &candidate(),
        )
        .unwrap();
}

#[test]
fn malformed_or_unbounded_artifacts_and_timing_fail_closed() {
    let request = request(AutomationFeature::Summaries);
    let mut receipt = summary_receipt();
    receipt.output.bytes = 0;
    assert!(
        receipt
            .validate_against(
                &request,
                &policy(AutomationFeature::Summaries),
                &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );

    let mut late = summary_receipt();
    late.completed_at_unix_ms = late.started_at_unix_ms + 86_400_001;
    assert!(
        late.validate_against(
            &request,
            &policy(AutomationFeature::Summaries),
            &adapter(AutomationFeature::Summaries, AdapterLocation::Local),
            &candidate(),
        )
        .is_err()
    );
}

#[test]
fn execution_contract_rejects_secret_or_prompt_fields() {
    let mut value = serde_json::to_value(request(AutomationFeature::Summaries)).unwrap();
    value.as_object_mut().unwrap().insert(
        "credential".into(),
        serde_json::Value::String("secret".into()),
    );
    assert!(serde_json::from_value::<AutomationExecutionRequest>(value).is_err());
}

#[test]
fn translation_receipt_must_match_the_requested_target_language() {
    let mut request = request(AutomationFeature::Translation);
    request.requested_output_language = Some("es-US".into());
    request.input.kind = AutomationInputKind::TextDocument;

    let mut receipt = summary_receipt();
    receipt.feature = AutomationFeature::Translation;
    receipt.input = request.input.clone();
    receipt.output.language = Some("fr-FR".into());
    receipt.facts = AutomationOutputFacts::Translation {
        source_language: "en-US".into(),
        target_language: "fr-FR".into(),
        translated_units: 120,
    };

    assert!(
        receipt
            .validate_against(
                &request,
                &policy(AutomationFeature::Translation),
                &adapter(AutomationFeature::Translation, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );
}

#[test]
fn caption_execution_requires_exact_media_input() {
    let request = request(AutomationFeature::Captions);
    let mut receipt = summary_receipt();
    receipt.feature = AutomationFeature::Captions;
    receipt.input = request.input.clone();
    receipt.output.language = Some("en-US".into());
    receipt.facts = AutomationOutputFacts::Captions {
        language: "en-US".into(),
        cue_count: 12,
        text_sha256: OUTPUT_SHA.into(),
        first_offset_ms: 0,
        last_end_ms: 60_000,
    };

    assert!(
        receipt
            .validate_against(
                &request,
                &policy(AutomationFeature::Captions),
                &adapter(AutomationFeature::Captions, AdapterLocation::Local),
                &candidate(),
            )
            .is_err()
    );
}
