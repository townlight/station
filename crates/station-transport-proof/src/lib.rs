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
    /// Digest of the bounded `tstables --json-output` artifact which was
    /// correlated with this analyzer report.
    pub tables_output_sha256: String,
    /// The one coherent PAT -> service -> PMT path accepted for this proof.
    pub service_id: u64,
    pub pmt_pid: u64,
    pub pcr_pid: u64,
    pub video_pid: u64,
    pub audio_pid: u64,
}

impl TransportFindings {
    pub fn validate(&self) -> Result<(), String> {
        let capture_bytes = u64::from(self.packet_size_bytes)
            .checked_mul(self.packet_count)
            .ok_or_else(|| {
                "transport analysis capture size overflows the supported range".to_string()
            })?;
        if self.packet_size_bytes == 0
            || self.packet_count == 0
            || self.capture_duration_ms == 0
            || self.capture_duration_ms > MAX_PROOF_DURATION_MS
            || capture_bytes > MAX_PROOF_CAPTURE_BYTES
            || self.bitrate_bps == 0
            || self.pat_program_count == 0
            || self.pmt_stream_count == 0
            || !sha256(&self.analyzer_output_sha256)
            || !sha256(&self.tables_output_sha256)
            || self.service_id == 0
            || self.service_id > 0xffff
            || self.pmt_pid == 0
            || self.pcr_pid == 0
            || self.video_pid == 0
            || self.audio_pid == 0
            || self.pmt_pid > 0x1fff
            || self.pcr_pid > 0x1fff
            || self.video_pid > 0x1fff
            || self.audio_pid > 0x1fff
            || self.pmt_pid == self.pcr_pid
            || self.pmt_pid == self.video_pid
            || self.pmt_pid == self.audio_pid
            || self.video_pid == self.audio_pid
            || self.pmt_pid == 0x1fff
            || self.pcr_pid == 0x1fff
            || self.video_pid == 0x1fff
            || self.audio_pid == 0x1fff
        {
            return Err("transport analysis findings are incomplete, oversized, or invalid".into());
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
    /// Exact sealed capture identity, independently persisted on the job.
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub outcome: ProofOutcome,
    pub findings: TransportFindings,
    pub captured_at_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub recorded_by: String,
    pub detail: String,
}

impl TransportAnalysisReceipt {
    pub fn validate(&self) -> Result<(), String> {
        validate_common_binding(CommonProofBinding {
            proof_id: &self.proof_id,
            channel_id: &self.channel_id,
            sink_id: &self.sink_id,
            target_id: &self.target_id,
            candidate_version: &self.candidate_version,
            source_commit: &self.source_commit,
            configuration_hash: &self.configuration_hash,
            observed_at_unix_ms: self.captured_at_unix_ms,
            recorded_at_unix_ms: self.recorded_at_unix_ms,
            recorded_by: &self.recorded_by,
            detail: &self.detail,
        })?;
        bounded_token(&self.analyzer_id, 100, "analyzer id")?;
        bounded_text(&self.analyzer_version, 100, "analyzer version")?;
        if !sha256(&self.analyzer_sha256)
            || self.capture_bytes == 0
            || self.capture_bytes > MAX_PROOF_CAPTURE_BYTES
            || !sha256(&self.capture_sha256)
        {
            return Err("analyzer executable hash is invalid".into());
        }
        if self.target_revision == 0 {
            return Err("transport analysis target revision is invalid".into());
        }
        match self.outcome {
            ProofOutcome::Pass => {
                self.findings.validate()?;
                let analyzed_bytes = u64::from(self.findings.packet_size_bytes)
                    .checked_mul(self.findings.packet_count)
                    .ok_or_else(|| {
                        "transport analysis capture size overflows the supported range".to_string()
                    })?;
                if analyzed_bytes != self.capture_bytes {
                    return Err(
                        "transport analysis packet facts do not match the sealed capture".into(),
                    );
                }
                Ok(())
            }
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
        validate_common_binding(CommonProofBinding {
            proof_id: &self.proof_id,
            channel_id: &self.channel_id,
            sink_id: &self.sink_id,
            target_id: &self.target_id,
            candidate_version: &self.candidate_version,
            source_commit: &self.source_commit,
            configuration_hash: &self.configuration_hash,
            observed_at_unix_ms: self.observed_at_unix_ms,
            recorded_at_unix_ms: self.recorded_at_unix_ms,
            recorded_by: &self.recorded_by,
            detail: &self.detail,
        })?;
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

pub struct CableTransportValidationInput<'a> {
    pub channel_id: &'a str,
    pub sink_id: &'a str,
    pub target: &'a TargetDeviceConfig,
    pub candidate_version: &'a str,
    pub source_commit: &'a str,
    pub configuration_hash: &'a str,
    pub now_unix_ms: i64,
    pub analysis: Option<TransportAnalysisReceipt>,
    pub reachability: Option<TargetDeviceReachabilityReceipt>,
}

impl CableTransportValidation {
    pub fn derive(input: CableTransportValidationInput<'_>) -> Result<Self, String> {
        let CableTransportValidationInput {
            channel_id,
            sink_id,
            target,
            candidate_version,
            source_commit,
            configuration_hash,
            now_unix_ms,
            analysis,
            reachability,
        } = input;
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

struct CommonProofBinding<'a> {
    proof_id: &'a str,
    channel_id: &'a str,
    sink_id: &'a str,
    target_id: &'a str,
    candidate_version: &'a str,
    source_commit: &'a str,
    configuration_hash: &'a str,
    observed_at_unix_ms: i64,
    recorded_at_unix_ms: i64,
    recorded_by: &'a str,
    detail: &'a str,
}

fn validate_common_binding(binding: CommonProofBinding<'_>) -> Result<(), String> {
    let CommonProofBinding {
        proof_id,
        channel_id,
        sink_id,
        target_id,
        candidate_version,
        source_commit,
        configuration_hash,
        observed_at_unix_ms,
        recorded_at_unix_ms,
        recorded_by,
        detail,
    } = binding;
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
    if value.is_empty() || value.len() > MAX_HOST || value.trim() != value {
        return Err("target host must be a normalized DNS name or IPv4 address".into());
    }
    let labels = value.split('.').collect::<Vec<_>>();
    if labels.len() == 4
        && labels
            .iter()
            .all(|label| !label.is_empty() && label.bytes().all(|byte| byte.is_ascii_digit()))
    {
        if labels.iter().any(|label| {
            (label.len() > 1 && label.starts_with('0')) || label.parse::<u8>().is_err()
        }) {
            return Err("target host must be a normalized DNS name or IPv4 address".into());
        }
        return Ok(());
    }
    if labels.iter().any(|label| {
        label.is_empty()
            || label.len() > 63
            || label.starts_with('-')
            || label.ends_with('-')
            || label
                .bytes()
                .any(|byte| !(byte.is_ascii_alphanumeric() || byte == b'-'))
    }) {
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
    if !(0..=TRANSPORT_PROOF_FRESHNESS_MS).contains(&age) {
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
            tables_output_sha256: "f".repeat(64),
            service_id: 1,
            pmt_pid: 4096,
            pcr_pid: 256,
            video_pid: 256,
            audio_pid: 257,
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
            capture_bytes: 188,
            capture_sha256: "e".repeat(64),
            outcome: ProofOutcome::Pass,
            findings: FakeTransportAnalyzer
                .analyze(&[0u8; 188])
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

    fn derive_validation(
        target: &TargetDeviceConfig,
        candidate_version: &str,
        now_unix_ms: i64,
        analysis: Option<TransportAnalysisReceipt>,
        reachability: Option<TargetDeviceReachabilityReceipt>,
    ) -> Result<CableTransportValidation, String> {
        CableTransportValidation::derive(CableTransportValidationInput {
            channel_id: "channel-1",
            sink_id: "sink-1",
            target,
            candidate_version,
            source_commit: &"a".repeat(40),
            configuration_hash: &"b".repeat(64),
            now_unix_ms,
            analysis,
            reachability,
        })
    }

    #[test]
    fn composite_requires_both_current_proofs() {
        let t = target();
        let blocked =
            derive_validation(&t, "0.1.0", 2, Some(analysis()), None).expect("valid composite");
        assert_eq!(blocked.outcome, None);
        let pass = derive_validation(&t, "0.1.0", 2, Some(analysis()), Some(reachability()))
            .expect("valid composite");
        assert_eq!(pass.outcome, Some(ProofOutcome::Pass));
    }

    #[test]
    fn stale_candidate_or_target_revision_is_blocked() {
        let mut t = target();
        let mut r = reachability();
        r.target_revision = 2;
        assert_eq!(
            derive_validation(&t, "0.1.0", 2, Some(analysis()), Some(r))
                .expect("valid composite")
                .outcome,
            None
        );
        t.revision = 2;
        assert_eq!(
            derive_validation(&t, "0.2.0", 2, Some(analysis()), None)
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
    fn findings_reject_capture_duration_and_byte_count_overflows() {
        let mut findings = analysis().findings;
        findings.capture_duration_ms = MAX_PROOF_DURATION_MS + 1;
        assert!(findings.validate().is_err());

        let mut findings = analysis().findings;
        findings.packet_size_bytes = u32::MAX;
        findings.packet_count = u64::MAX;
        assert!(findings.validate().is_err());

        let mut findings = analysis().findings;
        findings.packet_size_bytes = 188;
        findings.packet_count = MAX_PROOF_CAPTURE_BYTES / 188 + 1;
        assert!(findings.validate().is_err());
    }

    #[test]
    fn passing_receipt_binds_packet_facts_to_the_sealed_capture_size() {
        let mut receipt = analysis();
        receipt.capture_bytes += 188;

        assert!(receipt.validate().is_err());
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
    fn target_host_requires_valid_ipv4_or_dns_labels() {
        for invalid in [
            "999.999.999.999",
            "192.168.1.256",
            "receiver..example.test",
            "-receiver.example.test",
            "receiver-.example.test",
            "receiver.-example.test",
            "receiver.example-.test",
        ] {
            let mut target = target();
            target.host = invalid.into();
            assert!(target.validate().is_err(), "{invalid}");
        }
        for valid in [
            "192.168.1.1",
            "receiver-1.example.test",
            "123",
            "1.2",
            "1.2.3",
        ] {
            let mut target = target();
            target.host = valid.into();
            assert!(target.validate().is_ok(), "{valid}");
        }
        let mut target = target();
        target.host = format!("{}.example.test", "a".repeat(64));
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
        let validation = derive_validation(
            &target(),
            "0.1.0",
            TRANSPORT_PROOF_FRESHNESS_MS + 3,
            Some(analysis()),
            Some(reachability()),
        )
        .expect("valid composite");
        assert_eq!(validation.outcome, None);
    }
}
