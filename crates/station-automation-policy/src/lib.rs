//! Pure, fail-closed automation-model policy contracts.
//!
//! Adapter calls, model downloads, credential storage, persistence, and execution
//! are deliberately outside this crate.

use serde::{Deserialize, Serialize};

const MAX_TEXT: usize = 200;
const MAX_MODEL_TEXT: usize = 120;
const MAX_ADAPTERS: usize = 64;
const MAX_POLICIES: usize = 3;
const MAX_EVIDENCE: usize = 128;
const MAX_TEST_WINDOW_MS: i64 = 86_400_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AutomationFeature {
    Captions,
    Summaries,
    Translation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdapterLocation {
    Local,
    Cloud,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialAvailability {
    NotRequired,
    Available,
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationAdapterDescriptor {
    pub adapter_id: String,
    pub location: AdapterLocation,
    pub model_key: String,
    pub model_identifier: String,
    pub supported_features: Vec<AutomationFeature>,
    pub credential_availability: CredentialAvailability,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdaptiveDefaultHint {
    pub minimum_memory_gb: u16,
    pub preferred_adapter_id: String,
    pub fallback_adapter_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationFeaturePolicy {
    pub policy_id: String,
    pub feature: AutomationFeature,
    pub operator_selected_adapter_id: Option<String>,
    pub adaptive_default: Option<AdaptiveDefaultHint>,
    pub configuration_hash: String,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationCandidateIdentity {
    pub version: String,
    pub source_commit: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionTestOutcome {
    Passed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationConnectionTestEvidence {
    pub evidence_id: String,
    pub policy_id: String,
    pub adapter_id: String,
    pub policy_revision: u64,
    pub adapter_revision: u64,
    pub configuration_hash: String,
    pub candidate: AutomationCandidateIdentity,
    pub outcome: ConnectionTestOutcome,
    pub tested_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
    pub detail: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeatureAutomationStatus {
    SelectionMissing,
    InvalidConfiguration,
    CredentialUnavailable,
    NotTested,
    Passed,
    Failed,
    StaleConfiguration,
    StaleCandidate,
    Expired,
    InvalidEvidence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AutomationReadinessStatus {
    Ready,
    Blocked,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationReadiness {
    pub captions: FeatureAutomationStatus,
    pub summaries: FeatureAutomationStatus,
    pub translation: FeatureAutomationStatus,
    pub overall: AutomationReadinessStatus,
}

impl AutomationAdapterDescriptor {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.adapter_id, "automation adapter identity")?;
        validate_visible(
            &self.model_key,
            MAX_MODEL_TEXT,
            "automation adapter model key",
        )?;
        validate_visible(
            &self.model_identifier,
            MAX_MODEL_TEXT,
            "automation adapter model identifier",
        )?;
        if self.supported_features.is_empty()
            || self.supported_features.len() > 3
            || has_duplicate_by(&self.supported_features, |feature| feature_key(*feature))
        {
            return Err("automation adapter capabilities must be nonempty and unique".into());
        }
        if self.location == AdapterLocation::Cloud
            && self.credential_availability == CredentialAvailability::NotRequired
        {
            return Err(
                "cloud automation adapters require an explicit credential availability fact".into(),
            );
        }
        if self.revision == 0 {
            return Err("automation adapter revision must be nonzero".into());
        }
        Ok(())
    }
}

impl AdaptiveDefaultHint {
    pub fn validate(&self) -> Result<(), String> {
        if self.minimum_memory_gb == 0 || self.minimum_memory_gb > 128 {
            return Err("adaptive default memory threshold must be between 1 and 128 GB".into());
        }
        validate_uuid(
            &self.preferred_adapter_id,
            "adaptive default preferred adapter identity",
        )?;
        validate_uuid(
            &self.fallback_adapter_id,
            "adaptive default fallback adapter identity",
        )?;
        if self.preferred_adapter_id == self.fallback_adapter_id {
            return Err("adaptive default adapters must be distinct".into());
        }
        Ok(())
    }
}

impl AutomationFeaturePolicy {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.policy_id, "automation policy identity")?;
        if let Some(adapter_id) = &self.operator_selected_adapter_id {
            validate_uuid(adapter_id, "operator-selected automation adapter identity")?;
        }
        if let Some(adaptive_default) = &self.adaptive_default {
            adaptive_default.validate()?;
        }
        if self.revision == 0 || !is_sha256(&self.configuration_hash) {
            return Err("automation policy revision or configuration hash is invalid".into());
        }
        Ok(())
    }
}

impl AutomationCandidateIdentity {
    pub fn validate(&self) -> Result<(), String> {
        validate_visible(&self.version, MAX_TEXT, "automation candidate version")?;
        if !is_source_commit(&self.source_commit) {
            return Err(
                "automation candidate source commit must be a 40-character lowercase hash".into(),
            );
        }
        Ok(())
    }
}

impl AutomationConnectionTestEvidence {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.evidence_id, "automation connection evidence identity")?;
        validate_uuid(
            &self.policy_id,
            "automation connection evidence policy identity",
        )?;
        validate_uuid(
            &self.adapter_id,
            "automation connection evidence adapter identity",
        )?;
        if self.policy_revision == 0
            || self.adapter_revision == 0
            || !is_sha256(&self.configuration_hash)
            || self.tested_at_unix_ms < 0
            || self.expires_at_unix_ms <= self.tested_at_unix_ms
            || self.expires_at_unix_ms - self.tested_at_unix_ms > MAX_TEST_WINDOW_MS
        {
            return Err("automation connection evidence is invalid or unbounded".into());
        }
        self.candidate.validate()?;
        validate_visible(
            &self.detail,
            MAX_TEXT,
            "automation connection evidence detail",
        )
    }
}

pub fn derive_feature_automation_status(
    policy: &AutomationFeaturePolicy,
    adapters: &[AutomationAdapterDescriptor],
    evidence: Option<&AutomationConnectionTestEvidence>,
    current_candidate: &AutomationCandidateIdentity,
    now_unix_ms: i64,
) -> FeatureAutomationStatus {
    if now_unix_ms < 0
        || policy.validate().is_err()
        || current_candidate.validate().is_err()
        || adapters.len() > MAX_ADAPTERS
        || has_duplicate_by(adapters, |adapter| adapter.adapter_id.as_str())
    {
        return FeatureAutomationStatus::InvalidConfiguration;
    }
    let Some(selected_adapter_id) = &policy.operator_selected_adapter_id else {
        return FeatureAutomationStatus::SelectionMissing;
    };
    let Some(adapter) = adapters
        .iter()
        .find(|adapter| adapter.adapter_id == *selected_adapter_id)
    else {
        return FeatureAutomationStatus::InvalidConfiguration;
    };
    if adapter.validate().is_err() || !adapter.supported_features.contains(&policy.feature) {
        return FeatureAutomationStatus::InvalidConfiguration;
    }
    if let Some(adaptive_default) = &policy.adaptive_default {
        for adapter_id in [
            &adaptive_default.preferred_adapter_id,
            &adaptive_default.fallback_adapter_id,
        ] {
            let Some(adaptive_adapter) = adapters
                .iter()
                .find(|adapter| adapter.adapter_id == *adapter_id)
            else {
                return FeatureAutomationStatus::InvalidConfiguration;
            };
            if adaptive_adapter.validate().is_err()
                || !adaptive_adapter
                    .supported_features
                    .contains(&policy.feature)
            {
                return FeatureAutomationStatus::InvalidConfiguration;
            }
        }
    }
    if adapter.credential_availability == CredentialAvailability::Unavailable {
        return FeatureAutomationStatus::CredentialUnavailable;
    }
    let Some(evidence) = evidence else {
        return FeatureAutomationStatus::NotTested;
    };
    if evidence.validate().is_err()
        || evidence.policy_id != policy.policy_id
        || evidence.adapter_id != adapter.adapter_id
        || evidence.tested_at_unix_ms > now_unix_ms
    {
        return FeatureAutomationStatus::InvalidEvidence;
    }
    if evidence.policy_revision != policy.revision
        || evidence.adapter_revision != adapter.revision
        || evidence.configuration_hash != policy.configuration_hash
    {
        return FeatureAutomationStatus::StaleConfiguration;
    }
    if evidence.candidate != *current_candidate {
        return FeatureAutomationStatus::StaleCandidate;
    }
    if evidence.expires_at_unix_ms <= now_unix_ms {
        return FeatureAutomationStatus::Expired;
    }
    match evidence.outcome {
        ConnectionTestOutcome::Passed => FeatureAutomationStatus::Passed,
        ConnectionTestOutcome::Failed => FeatureAutomationStatus::Failed,
    }
}

pub fn derive_automation_readiness(
    adapters: &[AutomationAdapterDescriptor],
    policies: &[AutomationFeaturePolicy],
    evidence: &[AutomationConnectionTestEvidence],
    current_candidate: &AutomationCandidateIdentity,
    now_unix_ms: i64,
) -> Result<AutomationReadiness, String> {
    if now_unix_ms < 0 {
        return Err("automation readiness time must be nonnegative".into());
    }
    current_candidate.validate()?;
    if adapters.len() > MAX_ADAPTERS
        || policies.len() > MAX_POLICIES
        || evidence.len() > MAX_EVIDENCE
    {
        return Err("automation policy authority exceeds bounded record counts".into());
    }
    validate_unique_adapters(adapters)?;
    validate_unique_policies(policies)?;
    validate_unique_evidence(evidence)?;

    let captions = feature_status(
        AutomationFeature::Captions,
        adapters,
        policies,
        evidence,
        current_candidate,
        now_unix_ms,
    );
    let summaries = feature_status(
        AutomationFeature::Summaries,
        adapters,
        policies,
        evidence,
        current_candidate,
        now_unix_ms,
    );
    let translation = feature_status(
        AutomationFeature::Translation,
        adapters,
        policies,
        evidence,
        current_candidate,
        now_unix_ms,
    );
    let overall = readiness_status(&[captions, summaries, translation]);
    Ok(AutomationReadiness {
        captions,
        summaries,
        translation,
        overall,
    })
}

fn feature_status(
    feature: AutomationFeature,
    adapters: &[AutomationAdapterDescriptor],
    policies: &[AutomationFeaturePolicy],
    evidence: &[AutomationConnectionTestEvidence],
    current_candidate: &AutomationCandidateIdentity,
    now_unix_ms: i64,
) -> FeatureAutomationStatus {
    let Some(policy) = policies.iter().find(|policy| policy.feature == feature) else {
        return FeatureAutomationStatus::SelectionMissing;
    };
    derive_feature_automation_status(
        policy,
        adapters,
        evidence
            .iter()
            .find(|evidence| evidence.policy_id == policy.policy_id),
        current_candidate,
        now_unix_ms,
    )
}

fn readiness_status(statuses: &[FeatureAutomationStatus]) -> AutomationReadinessStatus {
    if statuses
        .iter()
        .all(|status| *status == FeatureAutomationStatus::Passed)
    {
        AutomationReadinessStatus::Ready
    } else if statuses.iter().any(|status| {
        matches!(
            status,
            FeatureAutomationStatus::SelectionMissing
                | FeatureAutomationStatus::InvalidConfiguration
                | FeatureAutomationStatus::CredentialUnavailable
                | FeatureAutomationStatus::Failed
                | FeatureAutomationStatus::StaleConfiguration
                | FeatureAutomationStatus::StaleCandidate
                | FeatureAutomationStatus::Expired
                | FeatureAutomationStatus::InvalidEvidence
        )
    }) {
        AutomationReadinessStatus::Blocked
    } else {
        AutomationReadinessStatus::Unknown
    }
}

fn validate_unique_adapters(adapters: &[AutomationAdapterDescriptor]) -> Result<(), String> {
    for adapter in adapters {
        adapter.validate()?;
    }
    if has_duplicate_by(adapters, |adapter| adapter.adapter_id.as_str()) {
        return Err("automation adapter identities must be unique".into());
    }
    Ok(())
}

fn validate_unique_policies(policies: &[AutomationFeaturePolicy]) -> Result<(), String> {
    for policy in policies {
        policy.validate()?;
    }
    if has_duplicate_by(policies, |policy| policy.policy_id.as_str())
        || has_duplicate_by(policies, |policy| feature_key(policy.feature))
    {
        return Err("automation policy identities and features must be unique".into());
    }
    Ok(())
}

fn validate_unique_evidence(evidence: &[AutomationConnectionTestEvidence]) -> Result<(), String> {
    for item in evidence {
        item.validate()?;
    }
    if has_duplicate_by(evidence, |item| item.evidence_id.as_str())
        || has_duplicate_by(evidence, |item| item.policy_id.as_str())
    {
        return Err(
            "automation connection evidence identities and policy snapshots must be unique".into(),
        );
    }
    Ok(())
}

fn has_duplicate_by<T, F>(values: &[T], key: F) -> bool
where
    F: Fn(&T) -> &str,
{
    values
        .iter()
        .enumerate()
        .any(|(index, value)| values[..index].iter().any(|prior| key(prior) == key(value)))
}

fn feature_key(feature: AutomationFeature) -> &'static str {
    match feature {
        AutomationFeature::Captions => "captions",
        AutomationFeature::Summaries => "summaries",
        AutomationFeature::Translation => "translation",
    }
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            index != 8
                && index != 13
                && index != 18
                && index != 23
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_visible(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_source_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
