//! Truthful commissioning evidence for transport analysis and target-device reachability.
//!
//! This crate deliberately contains no process, network, or TSDuck integration.  It
//! defines the bounded, immutable facts that a future trusted executor must produce.
//! A commissioning pass requires both independent facts, bound to the same current
//! candidate, output configuration, sink, and target-device revision.

use serde::{Deserialize, Serialize};
#[cfg(test)]
use sha2::{Digest, Sha256};

pub const MAX_LABEL: usize = 200;
pub const MAX_HOST: usize = 253;
pub const MAX_DETAIL: usize = 2_000;
pub const MAX_PROOF_DURATION_MS: u64 = 600_000;
pub const MAX_PROOF_CAPTURE_BYTES: u64 = 1_073_741_824;
pub const TRANSPORT_PROOF_FRESHNESS_MS: i64 = 24 * 60 * 60 * 1_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProofOutcome {
    Pass,
    Fail,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetProtocol {
    HttpsApi,
    SnmpTls,
}

impl TargetProtocol {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::HttpsApi => "https_api",
            Self::SnmpTls => "snmp_tls",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TargetDeviceConfig {
    pub target_id: String,
    pub label: String,
    pub protocol: TargetProtocol,
    /// DNS name or IPv4 address only. Paths, queries, user information, and
    /// credentials are structurally impossible in this field.
    pub host: String,
    pub port: u16,
    pub credential_configured: bool,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

impl TargetDeviceConfig {
    pub fn validate(&self) -> Result<(), String> {
        bounded_token(&self.target_id, 100, "target id")?;
        bounded_text(&self.label, MAX_LABEL, "target label")?;
        validate_host(&self.host)?;
        bounded_text(&self.updated_by, MAX_LABEL, "target attribution")?;
        if self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
        {
            return Err("target configuration revision and timestamps are invalid".into());
        }
        if self.port == 0 {
            return Err("target port is invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransportFindings {
    pub packet_size_bytes: u32,
    pub packet_count: u64,
    pub capture_duration_ms: u64,
    pub bitrate_bps: u64,
    pub pat_program_count: u32,
    pub pmt_stream_count: u32,
    pub continuity_errors: u64,
    pub pcr_errors: u64,
    pub analyzer_output_sha256: String,
}

impl TransportFindings {
    pub fn validate(&self) -> Result<(), String> {
        if self.packet_size_bytes == 0
            || self.packet_count == 0
            || self.capture_duration_ms == 0
            || self.bitrate_bps == 0
            || self.pat_program_count == 0
            || self.pmt_stream_count == 0
            || !sha256(&self.analyzer_output_sha256)
        {
            return Err("transport analysis findings are incomplete or invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransportAnalysisReceipt {
    pub proof_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub target_id: String,
    pub target_revision: u64,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
    pub analyzer_id: String,
    pub analyzer_version: String,
    pub analyzer_sha256: String,
    pub outcome: ProofOutcome,
    pub findings: TransportFindings,
    pub captured_at_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub recorded_by: String,
    pub detail: String,
}

impl TransportAnalysisReceipt {
    pub fn validate(&self) -> Result<(), String> {
        validate_common_binding(
            &self.proof_id,
            &self.channel_id,
            &self.sink_id,
            &self.target_id,
            &self.candidate_version,
            &self.source_commit,
            &self.configuration_hash,
            self.captured_at_unix_ms,
            self.recorded_at_unix_ms,
            &self.recorded_by,
            &self.detail,
        )?;
        bounded_token(&self.analyzer_id, 100, "analyzer id")?;
        bounded_text(&self.analyzer_version, 100, "analyzer version")?;
        if !sha256(&self.analyzer_sha256) {
            return Err("analyzer executable hash is invalid".into());
        }
        if self.target_revision == 0 {
            return Err("transport analysis target revision is invalid".into());
        }
        match self.outcome {
            ProofOutcome::Pass => self.findings.validate(),
            ProofOutcome::Fail => {
                if !sha256(&self.findings.analyzer_output_sha256) {
                    return Err(
                        "failed transport analysis must retain a bounded output digest".into(),
                    );
                }
                Ok(())
            }
        }
    }

    pub fn validate_fresh_at(&self, now_unix_ms: i64) -> Result<(), String> {
        self.validate()?;
        validate_freshness(self.captured_at_unix_ms, now_unix_ms, "transport analysis")?;
        validate_freshness(
            self.recorded_at_unix_ms,
            now_unix_ms,
            "transport analysis recording",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TargetDeviceReachabilityReceipt {
    pub proof_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub target_id: String,
    pub target_revision: u64,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
    pub adapter_id: String,
    pub adapter_version: String,
    pub auth_method: String,
    pub outcome: ProofOutcome,
    pub response_identity_sha256: String,
    pub latency_ms: u64,
    pub observed_at_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub recorded_by: String,
    pub detail: String,
}

impl TargetDeviceReachabilityReceipt {
    pub fn validate(&self) -> Result<(), String> {
        validate_common_binding(
            &self.proof_id,
            &self.channel_id,
            &self.sink_id,
            &self.target_id,
            &self.candidate_version,
            &self.source_commit,
            &self.configuration_hash,
            self.observed_at_unix_ms,
            self.recorded_at_unix_ms,
            &self.recorded_by,
            &self.detail,
        )?;
        bounded_token(&self.adapter_id, 100, "adapter id")?;
        bounded_text(&self.adapter_version, 100, "adapter version")?;
        bounded_token(&self.auth_method, 100, "authentication method")?;
        if self.target_revision == 0 || !sha256(&self.response_identity_sha256) {
            return Err("target reachability identity is invalid".into());
        }
        Ok(())
    }

    pub fn validate_fresh_at(&self, now_unix_ms: i64) -> Result<(), String> {
        self.validate()?;
        validate_freshness(self.observed_at_unix_ms, now_unix_ms, "target reachability")?;
        validate_freshness(
            self.recorded_at_unix_ms,
            now_unix_ms,
            "target reachability recording",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CableTransportValidation {
    pub channel_id: String,
    pub sink_id: String,
    pub target_id: String,
    pub target_revision: u64,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
    pub evaluated_at_unix_ms: i64,
    pub transport_analysis: Option<TransportAnalysisReceipt>,
    pub target_reachability: Option<TargetDeviceReachabilityReceipt>,
    pub outcome: Option<ProofOutcome>,
    pub detail: String,
}

impl CableTransportValidation {
    pub fn derive(
        channel_id: &str,
        sink_id: &str,
        target: &TargetDeviceConfig,
        candidate_version: &str,
        source_commit: &str,
        configuration_hash: &str,
        now_unix_ms: i64,
        analysis: Option<TransportAnalysisReceipt>,
        reachability: Option<TargetDeviceReachabilityReceipt>,
    ) -> Result<Self, String> {
        target.validate()?;
        bounded_token(channel_id, 100, "channel id")?;
        bounded_token(sink_id, 100, "sink id")?;
        if candidate_version.is_empty() || !sha256(configuration_hash) || source_commit.len() != 40
        {
            return Err("current transport validation binding is invalid".into());
        }
        if now_unix_ms < 0 {
            return Err("transport validation time is invalid".into());
        }
        let both_current = analysis.as_ref().is_some_and(|value| {
            value.outcome == ProofOutcome::Pass
                && value.channel_id == channel_id
                && value.sink_id == sink_id
                && value.target_id == target.target_id
                && value.target_revision == target.revision
                && value.candidate_version == candidate_version
                && value.source_commit == source_commit
                && value.configuration_hash == configuration_hash
                && value.validate_fresh_at(now_unix_ms).is_ok()
        }) && reachability.as_ref().is_some_and(|value| {
            value.outcome == ProofOutcome::Pass
                && value.channel_id == channel_id
                && value.sink_id == sink_id
                && value.target_id == target.target_id
                && value.target_revision == target.revision
                && value.candidate_version == candidate_version
                && value.source_commit == source_commit
                && value.configuration_hash == configuration_hash
                && value.validate_fresh_at(now_unix_ms).is_ok()
        });
        let detail = if both_current {
            "Current transport analysis and authenticated target-device reachability both pass."
                .into()
        } else {
            "Transport validation remains blocked until both current proofs pass for the exact target revision and candidate/configuration binding.".into()
        };
        Ok(Self {
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: target.target_id.clone(),
            target_revision: target.revision,
            candidate_version: candidate_version.into(),
            source_commit: source_commit.into(),
            configuration_hash: configuration_hash.into(),
            evaluated_at_unix_ms: now_unix_ms,
            transport_analysis: analysis,
            target_reachability: reachability,
            outcome: both_current.then_some(ProofOutcome::Pass),
            detail,
        })
    }
}

fn validate_common_binding(
    proof_id: &str,
    channel_id: &str,
    sink_id: &str,
    target_id: &str,
    candidate_version: &str,
    source_commit: &str,
    configuration_hash: &str,
    observed_at_unix_ms: i64,
    recorded_at_unix_ms: i64,
    recorded_by: &str,
    detail: &str,
) -> Result<(), String> {
    for (value, field) in [
        (proof_id, "proof id"),
        (channel_id, "channel id"),
        (sink_id, "sink id"),
        (target_id, "target id"),
    ] {
        bounded_token(value, 100, field)?;
    }
    bounded_text(candidate_version, 200, "candidate version")?;
    bounded_text(recorded_by, MAX_LABEL, "proof attribution")?;
    bounded_text(detail, MAX_DETAIL, "proof detail")?;
    if source_commit.len() != 40 || !source_commit.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("source commit identity is invalid".into());
    }
    if !sha256(configuration_hash)
        || observed_at_unix_ms < 0
        || recorded_at_unix_ms < observed_at_unix_ms
    {
        return Err("proof binding timestamps or configuration hash are invalid".into());
    }
    Ok(())
}

fn bounded_token(value: &str, max: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > max
        || value.trim() != value
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} is empty, oversized, or contains invalid characters"
        ));
    }
    Ok(())
}

fn bounded_text(value: &str, max: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > max
        || value.trim() != value
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} is empty, oversized, or contains invalid characters"
        ));
    }
    Ok(())
}

fn sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn validate_host(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > MAX_HOST
        || value.trim() != value
        || value.starts_with(['.', '-'])
        || value.ends_with(['.', '-'])
        || value
            .bytes()
            .any(|byte| !(byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-')))
    {
        return Err("target host must be a normalized DNS name or IPv4 address".into());
    }
    Ok(())
}

fn validate_freshness(
    recorded_at_unix_ms: i64,
    now_unix_ms: i64,
    authority: &str,
) -> Result<(), String> {
    let age = now_unix_ms
        .checked_sub(recorded_at_unix_ms)
        .ok_or_else(|| format!("{authority} time is outside the supported range"))?;
    if age < 0 || age > TRANSPORT_PROOF_FRESHNESS_MS {
        return Err(format!("{authority} evidence is future-dated or expired"));
    }
    Ok(())
}

/// A fake analyzer used only by unit tests and future executor integration tests.
#[cfg(test)]
pub trait TransportAnalyzer {
    fn analyze(&self, capture: &[u8]) -> Result<TransportFindings, String>;
}

/// A fake authenticated target adapter used only by tests. It returns an identity
/// digest only after its configured secret and challenge match.
#[cfg(test)]
pub trait AuthenticatedTargetAdapter {
    fn probe(&self, target: &TargetDeviceConfig, challenge: &[u8]) -> Result<String, String>;
}

#[derive(Debug, Clone, Copy)]
#[cfg(test)]
pub struct FakeTransportAnalyzer;

#[cfg(test)]
impl TransportAnalyzer for FakeTransportAnalyzer {
    fn analyze(&self, capture: &[u8]) -> Result<TransportFindings, String> {
        if capture.is_empty() {
            return Err("fake analyzer received an empty capture".into());
        }
        let output_hash = Sha256::digest(capture);
        Ok(TransportFindings {
            packet_size_bytes: 188,
            packet_count: (capture.len() / 188).max(1) as u64,
            capture_duration_ms: 1_000,
            bitrate_bps: 1_000_000,
            pat_program_count: 1,
            pmt_stream_count: 2,
            continuity_errors: 0,
            pcr_errors: 0,
            analyzer_output_sha256: format!("{output_hash:x}"),
        })
    }
}

#[derive(Debug, Clone)]
#[cfg(test)]
pub struct FakeAuthenticatedTargetAdapter {
    pub expected_secret: String,
}

#[cfg(test)]
impl AuthenticatedTargetAdapter for FakeAuthenticatedTargetAdapter {
    fn probe(&self, target: &TargetDeviceConfig, challenge: &[u8]) -> Result<String, String> {
        if !target.credential_configured || challenge.is_empty() {
            return Err("fake target rejected missing authentication material".into());
        }
        let mut digest = Sha256::new();
        digest.update(target.target_id.as_bytes());
        digest.update(self.expected_secret.as_bytes());
        digest.update(challenge);
        Ok(format!("{:x}", digest.finalize()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target() -> TargetDeviceConfig {
        TargetDeviceConfig {
            target_id: "target-1".into(),
            label: "Lab receiver".into(),
            protocol: TargetProtocol::HttpsApi,
            host: "receiver-1.example.test".into(),
            port: 443,
            credential_configured: true,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            updated_by: "operator-1".into(),
        }
    }

    fn analysis() -> TransportAnalysisReceipt {
        TransportAnalysisReceipt {
            proof_id: "analysis-1".into(),
            channel_id: "channel-1".into(),
            sink_id: "sink-1".into(),
            target_id: "target-1".into(),
            target_revision: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            configuration_hash: "b".repeat(64),
            analyzer_id: "fake-analyzer".into(),
            analyzer_version: "1".into(),
            analyzer_sha256: "c".repeat(64),
            outcome: ProofOutcome::Pass,
            findings: FakeTransportAnalyzer
                .analyze(&vec![0u8; 188])
                .expect("fake analysis"),
            captured_at_unix_ms: 2,
            recorded_at_unix_ms: 2,
            recorded_by: "worker".into(),
            detail: "ok".into(),
        }
    }

    fn reachability() -> TargetDeviceReachabilityReceipt {
        TargetDeviceReachabilityReceipt {
            proof_id: "reach-1".into(),
            channel_id: "channel-1".into(),
            sink_id: "sink-1".into(),
            target_id: "target-1".into(),
            target_revision: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            configuration_hash: "b".repeat(64),
            adapter_id: "fake-adapter".into(),
            adapter_version: "1".into(),
            auth_method: "challenge".into(),
            outcome: ProofOutcome::Pass,
            response_identity_sha256: "d".repeat(64),
            latency_ms: 3,
            observed_at_unix_ms: 2,
            recorded_at_unix_ms: 2,
            recorded_by: "worker".into(),
            detail: "ok".into(),
        }
    }

    #[test]
    fn composite_requires_both_current_proofs() {
        let t = target();
        let blocked = CableTransportValidation::derive(
            "channel-1",
            "sink-1",
            &t,
            "0.1.0",
            &"a".repeat(40),
            &"b".repeat(64),
            2,
            Some(analysis()),
            None,
        )
        .expect("valid composite");
        assert_eq!(blocked.outcome, None);
        let pass = CableTransportValidation::derive(
            "channel-1",
            "sink-1",
            &t,
            "0.1.0",
            &"a".repeat(40),
            &"b".repeat(64),
            2,
            Some(analysis()),
            Some(reachability()),
        )
        .expect("valid composite");
        assert_eq!(pass.outcome, Some(ProofOutcome::Pass));
    }

    #[test]
    fn stale_candidate_or_target_revision_is_blocked() {
        let mut t = target();
        let mut r = reachability();
        r.target_revision = 2;
        assert_eq!(
            CableTransportValidation::derive(
                "channel-1",
                "sink-1",
                &t,
                "0.1.0",
                &"a".repeat(40),
                &"b".repeat(64),
                2,
                Some(analysis()),
                Some(r)
            )
            .expect("valid composite")
            .outcome,
            None
        );
        t.revision = 2;
        assert_eq!(
            CableTransportValidation::derive(
                "channel-1",
                "sink-1",
                &t,
                "0.2.0",
                &"a".repeat(40),
                &"b".repeat(64),
                2,
                Some(analysis()),
                None
            )
            .expect("valid composite")
            .outcome,
            None
        );
    }

    #[test]
    fn fake_adapter_rejects_missing_authentication() {
        let mut t = target();
        t.credential_configured = false;
        assert!(
            FakeAuthenticatedTargetAdapter {
                expected_secret: "secret".into()
            }
            .probe(&t, b"nonce")
            .is_err()
        );
    }

    #[test]
    fn fake_adapter_authenticates_named_target_and_fake_analyzer_is_bounded() {
        let target = target();
        let adapter = FakeAuthenticatedTargetAdapter {
            expected_secret: "secret".into(),
        };
        let identity = adapter
            .probe(&target, b"nonce")
            .expect("authenticated target");
        assert_eq!(identity.len(), 64);
        let findings = FakeTransportAnalyzer
            .analyze(&vec![0_u8; 188 * 2])
            .expect("bounded capture");
        assert_eq!(findings.packet_size_bytes, 188);
        assert_eq!(findings.packet_count, 2);
    }

    #[test]
    fn target_endpoint_rejects_embedded_query_secrets() {
        let mut target = target();
        target.host = "receiver.example.test?token=secret".into();
        assert!(target.validate().is_err());
    }

    #[test]
    fn target_endpoint_rejects_path_token_leakage() {
        let mut target = target();
        target.host = "receiver.example.test/api/token/secret".into();
        assert!(target.validate().is_err());
    }

    #[test]
    fn fake_protocol_is_not_valid_production_configuration() {
        let value = serde_json::json!({
            "target_id": "target-1",
            "label": "Fake target",
            "protocol": "fake_authenticated_v1",
            "host": "receiver.example.test",
            "port": 443,
            "credential_configured": true,
            "revision": 1,
            "created_at_unix_ms": 1,
            "updated_at_unix_ms": 1,
            "updated_by": "operator-1"
        });
        assert!(serde_json::from_value::<TargetDeviceConfig>(value).is_err());
    }

    #[test]
    fn expired_evidence_cannot_pass_the_composite() {
        let validation = CableTransportValidation::derive(
            "channel-1",
            "sink-1",
            &target(),
            "0.1.0",
            &"a".repeat(40),
            &"b".repeat(64),
            TRANSPORT_PROOF_FRESHNESS_MS + 3,
            Some(analysis()),
            Some(reachability()),
        )
        .expect("valid composite");
        assert_eq!(validation.outcome, None);
    }
}
