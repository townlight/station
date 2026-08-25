use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_CAPTION_CUES_PER_TRACK: usize = 100_000;
pub const MAX_CAPTION_CUE_DURATION_MS: u64 = 60_000;
pub const MAX_CAPTION_CUE_TEXT_BYTES: usize = 2_048;
pub const MAX_CAPTION_PROOF_CAPTURE_DURATION_MS: u64 = 120_000;
pub const MAX_CAPTION_PROOF_CAPTURE_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaptionProofOutcome {
    Verified,
    Mismatch,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CaptionEgressProof {
    pub proof_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub report_id: String,
    pub asset_id: String,
    pub track_id: String,
    pub track_revision: u64,
    pub cues_sha256: String,
    pub caption_policy_revision: u64,
    pub language: String,
    pub service_number: u8,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
    pub capture_started_asset_offset_ms: u64,
    pub capture_duration_ms: u64,
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub artifact_relative_path: String,
    pub expected_cue_count: u32,
    pub decoded_cue_count: u32,
    pub expected_text_sha256: String,
    pub decoded_text_sha256: String,
    pub decoder_engine: String,
    pub decoder_method: String,
    pub outcome: CaptionProofOutcome,
    pub captured_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExpectedCaptionEvidence {
    pub cue_count: u32,
    pub text_sha256: String,
}

impl CaptionEgressProof {
    pub fn validate(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.proof_id, "caption proof identity"),
            (&self.channel_id, "caption proof channel identity"),
            (&self.sink_id, "caption proof sink identity"),
            (&self.track_id, "caption proof track identity"),
        ] {
            if !is_canonical_uuid(value) {
                return Err(format!("{field} must be a lowercase canonical UUID"));
            }
        }
        validate_visible(
            &self.report_id,
            200,
            "caption proof schedule report identity",
        )?;
        for (value, field) in [
            (&self.asset_id, "caption proof asset identity"),
            (&self.cues_sha256, "caption proof cue digest"),
            (&self.configuration_hash, "caption proof configuration hash"),
            (&self.capture_sha256, "caption proof capture hash"),
            (
                &self.expected_text_sha256,
                "caption proof expected text hash",
            ),
            (&self.decoded_text_sha256, "caption proof decoded text hash"),
        ] {
            if !is_sha256_identity(value) {
                return Err(format!("{field} must be a lowercase SHA-256 digest"));
            }
        }
        if self.source_commit.len() != 40
            || !self
                .source_commit
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(
                "caption proof source commit must be an exact lowercase commit hash".into(),
            );
        }
        if self.track_revision == 0 || self.caption_policy_revision == 0 {
            return Err("caption proof authority revisions must be nonzero".into());
        }
        if !is_language_tag(&self.language) || !(1..=63).contains(&self.service_number) {
            return Err("caption proof language or CEA-708 service is invalid".into());
        }
        validate_visible(
            &self.candidate_version,
            200,
            "caption proof candidate version",
        )?;
        validate_visible(&self.decoder_engine, 200, "caption proof decoder engine")?;
        validate_visible(&self.decoder_method, 500, "caption proof decoder method")?;
        if self
            .capture_started_asset_offset_ms
            .checked_add(self.capture_duration_ms)
            .is_none()
            || !(1..=MAX_CAPTION_PROOF_CAPTURE_DURATION_MS).contains(&self.capture_duration_ms)
            || !(1..=MAX_CAPTION_PROOF_CAPTURE_BYTES).contains(&self.capture_bytes)
            || !(1..=MAX_CAPTION_CUES_PER_TRACK as u32).contains(&self.expected_cue_count)
            || !(1..=MAX_CAPTION_CUES_PER_TRACK as u32).contains(&self.decoded_cue_count)
            || self.captured_at_unix_ms < 0
        {
            return Err("caption proof capture facts are invalid or unbounded".into());
        }
        let expected_artifact = format!("caption-proofs/{}.ts", self.proof_id);
        if self.artifact_relative_path != expected_artifact {
            return Err("caption proof artifact path is not the proof-owned managed path".into());
        }
        let evidence_matches = self.expected_cue_count == self.decoded_cue_count
            && self.expected_text_sha256 == self.decoded_text_sha256;
        match self.outcome {
            CaptionProofOutcome::Verified if !evidence_matches => {
                Err("verified caption proof does not match independently decoded evidence".into())
            }
            CaptionProofOutcome::Mismatch if evidence_matches => {
                Err("caption proof mismatch outcome contradicts matching evidence".into())
            }
            _ => Ok(()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CaptionTrackSourceKind {
    Embedded,
    Sidecar,
    Generated,
    External,
}

impl CaptionTrackSourceKind {
    pub const fn storage_name(self) -> &'static str {
        match self {
            Self::Embedded => "embedded",
            Self::Sidecar => "sidecar",
            Self::Generated => "generated",
            Self::External => "external",
        }
    }

    pub fn from_storage(value: &str) -> Result<Self, String> {
        match value {
            "embedded" => Ok(Self::Embedded),
            "sidecar" => Ok(Self::Sidecar),
            "generated" => Ok(Self::Generated),
            "external" => Ok(Self::External),
            _ => Err("database returned an invalid caption track source".into()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetCaptionCue {
    pub cue_id: String,
    pub start_ms: u64,
    pub duration_ms: u64,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AssetCaptionTrackWrite {
    pub track_id: String,
    pub asset_id: String,
    pub language: String,
    pub service_number: u8,
    pub source_kind: CaptionTrackSourceKind,
    pub label: String,
    pub cues: Vec<AssetCaptionCue>,
    pub expected_revision: u64,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
    pub audit_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetCaptionTrack {
    pub track_id: String,
    pub asset_id: String,
    pub language: String,
    pub service_number: u8,
    pub source_kind: CaptionTrackSourceKind,
    pub label: String,
    pub cues: Vec<AssetCaptionCue>,
    pub revision: u64,
    pub cues_sha256: String,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetCaptionTrackAuditEvent {
    pub audit_id: String,
    pub track_id: String,
    pub asset_id: String,
    pub language: String,
    pub service_number: u8,
    pub source_kind: CaptionTrackSourceKind,
    pub label: String,
    pub cues: Vec<AssetCaptionCue>,
    pub revision: u64,
    pub cues_sha256: String,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptionTrackWriteError {
    AssetNotFound,
    AssetNotReady,
    AssetOnAir,
    NotFound,
    Conflict(u64),
    SlotOccupied { track_id: String, revision: u64 },
    Invalid(String),
    Storage(String),
}

impl AssetCaptionTrackWrite {
    pub fn validate(&self, asset_duration_ms: u64) -> Result<(), String> {
        validate_track_metadata(
            &self.track_id,
            &self.asset_id,
            &self.language,
            self.service_number,
            &self.label,
        )?;
        validate_visible(&self.actor, 200, "caption track actor")?;
        if self.occurred_at_unix_ms < 0 {
            return Err("caption track write time must be nonnegative".into());
        }
        if !is_canonical_uuid(&self.audit_id) {
            return Err("caption track audit identity must be a lowercase canonical UUID".into());
        }
        validate_cues(&self.cues, asset_duration_ms)
    }

    pub fn cues_sha256(&self) -> String {
        cues_sha256(&self.cues)
    }
}

impl AssetCaptionTrack {
    pub fn validate(&self, asset_duration_ms: u64) -> Result<(), String> {
        validate_track_metadata(
            &self.track_id,
            &self.asset_id,
            &self.language,
            self.service_number,
            &self.label,
        )?;
        validate_cues(&self.cues, asset_duration_ms)?;
        if self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
        {
            return Err("caption track revision or timestamps are invalid".into());
        }
        validate_visible(&self.updated_by, 200, "caption track actor")?;
        if self.cues_sha256 != cues_sha256(&self.cues) {
            return Err("caption track cue digest does not match its cues".into());
        }
        Ok(())
    }
}

impl AssetCaptionTrackAuditEvent {
    pub fn validate(&self, asset_duration_ms: u64) -> Result<(), String> {
        validate_track_metadata(
            &self.track_id,
            &self.asset_id,
            &self.language,
            self.service_number,
            &self.label,
        )?;
        validate_cues(&self.cues, asset_duration_ms)?;
        if !is_canonical_uuid(&self.audit_id) || self.revision == 0 || self.occurred_at_unix_ms < 0
        {
            return Err("caption track audit identity, revision, or time is invalid".into());
        }
        validate_visible(&self.actor, 200, "caption track actor")?;
        if self.cues_sha256 != cues_sha256(&self.cues) {
            return Err("caption track audit cue digest does not match its cues".into());
        }
        Ok(())
    }
}

pub fn cues_sha256(cues: &[AssetCaptionCue]) -> String {
    let mut digest = Sha256::new();
    digest.update(b"townlight.asset-caption-cues.v1\0");
    digest.update((cues.len() as u64).to_be_bytes());
    for (ordinal, cue) in cues.iter().enumerate() {
        digest.update((ordinal as u64).to_be_bytes());
        update_length_prefixed(&mut digest, cue.cue_id.as_bytes());
        digest.update(cue.start_ms.to_be_bytes());
        digest.update(cue.duration_ms.to_be_bytes());
        update_length_prefixed(&mut digest, cue.text.as_bytes());
    }
    format!("{:x}", digest.finalize())
}

pub fn canonical_caption_text_sha256(texts: &[String]) -> Result<String, String> {
    if texts.is_empty() || texts.len() > MAX_CAPTION_CUES_PER_TRACK {
        return Err("caption proof text set must be bounded and nonempty".into());
    }
    let mut digest = Sha256::new();
    digest.update(b"townlight.caption-proof-text.v1\0");
    digest.update((texts.len() as u64).to_be_bytes());
    for text in texts {
        if text.is_empty()
            || text.len() > MAX_CAPTION_CUE_TEXT_BYTES
            || text
                .chars()
                .any(|character| character.is_control() && !character.is_whitespace())
        {
            return Err("caption proof text is empty, unbounded, or contains controls".into());
        }
        let canonical = text.split_whitespace().collect::<Vec<_>>().join(" ");
        if canonical.is_empty() {
            return Err("caption proof text is empty after whitespace normalization".into());
        }
        update_length_prefixed(&mut digest, canonical.as_bytes());
    }
    Ok(format!("{:x}", digest.finalize()))
}

pub fn expected_caption_evidence_for_window(
    track: &AssetCaptionTrack,
    asset_duration_ms: u64,
    capture_started_asset_offset_ms: u64,
    capture_duration_ms: u64,
) -> Result<ExpectedCaptionEvidence, String> {
    track.validate(asset_duration_ms)?;
    if !(1..=MAX_CAPTION_PROOF_CAPTURE_DURATION_MS).contains(&capture_duration_ms) {
        return Err("caption proof window duration is invalid or unbounded".into());
    }
    let capture_end = capture_started_asset_offset_ms
        .checked_add(capture_duration_ms)
        .ok_or_else(|| "caption proof window overflowed".to_string())?;
    if capture_end > asset_duration_ms {
        return Err("caption proof window extends beyond the bound media asset".into());
    }
    let texts = track
        .cues
        .iter()
        .filter(|cue| {
            cue.start_ms + cue.duration_ms > capture_started_asset_offset_ms
                && cue.start_ms < capture_end
        })
        .map(|cue| cue.text.clone())
        .collect::<Vec<_>>();
    let cue_count = u32::try_from(texts.len())
        .map_err(|_| "caption proof expected cue count overflowed".to_string())?;
    let text_sha256 = canonical_caption_text_sha256(&texts)?;
    Ok(ExpectedCaptionEvidence {
        cue_count,
        text_sha256,
    })
}

fn update_length_prefixed(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value);
}

fn validate_track_metadata(
    track_id: &str,
    asset_id: &str,
    language: &str,
    service_number: u8,
    label: &str,
) -> Result<(), String> {
    if !is_canonical_uuid(track_id) {
        return Err("caption track identity must be a lowercase canonical UUID".into());
    }
    if !is_sha256_identity(asset_id) {
        return Err("caption asset identity must be a lowercase SHA-256 digest".into());
    }
    if !is_language_tag(language) {
        return Err("caption language must be a bounded BCP-47-style tag".into());
    }
    if !(1..=63).contains(&service_number) {
        return Err("caption service number must be between 1 and 63".into());
    }
    validate_visible(label, 120, "caption track label")
}

fn validate_cues(cues: &[AssetCaptionCue], asset_duration_ms: u64) -> Result<(), String> {
    if cues.is_empty() || cues.len() > MAX_CAPTION_CUES_PER_TRACK {
        return Err("caption track must contain a bounded nonempty cue set".into());
    }
    let mut prior_end_ms = 0;
    let mut cue_ids = HashSet::with_capacity(cues.len());
    for (index, cue) in cues.iter().enumerate() {
        if !is_canonical_uuid(&cue.cue_id) || !cue_ids.insert(cue.cue_id.as_str()) {
            return Err("caption cue identities must be unique lowercase canonical UUIDs".into());
        }
        if !(1..=MAX_CAPTION_CUE_DURATION_MS).contains(&cue.duration_ms) {
            return Err("caption cue duration is outside the bounded range".into());
        }
        if cue.text.is_empty()
            || cue.text.trim() != cue.text
            || cue.text.len() > MAX_CAPTION_CUE_TEXT_BYTES
            || cue
                .text
                .chars()
                .any(|character| character.is_control() && character != '\n')
        {
            return Err("caption cue text is empty, unbounded, or contains controls".into());
        }
        if index > 0 && cue.start_ms < prior_end_ms {
            return Err("caption cues must be ordered and non-overlapping".into());
        }
        prior_end_ms = cue
            .start_ms
            .checked_add(cue.duration_ms)
            .ok_or_else(|| "caption cue timing overflowed".to_string())?;
        if prior_end_ms > asset_duration_ms {
            return Err("caption cue extends beyond the bound media asset".into());
        }
    }
    Ok(())
}

fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            [8, 13, 18, 23].contains(&index)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(byte)
        })
}

fn is_sha256_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_language_tag(value: &str) -> bool {
    let bytes = value.as_bytes();
    (2..=35).contains(&bytes.len())
        && bytes.first().is_some_and(u8::is_ascii_alphabetic)
        && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
        && !value.contains("--")
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

#[cfg(test)]
mod tests {
    use super::*;

    fn write(cues: Vec<AssetCaptionCue>) -> AssetCaptionTrackWrite {
        AssetCaptionTrackWrite {
            track_id: "65a26343-0339-4654-a92c-4bf9384f18df".into(),
            asset_id: "a".repeat(64),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::External,
            label: "Reviewed English".into(),
            cues,
            expected_revision: 0,
            actor: "setup-admin".into(),
            occurred_at_unix_ms: 1_000,
            audit_id: "54691f24-e034-45bd-9ed5-31d9227a78aa".into(),
        }
    }

    fn cue() -> AssetCaptionCue {
        AssetCaptionCue {
            cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
            start_ms: 1_000,
            duration_ms: 2_000,
            text: "Council meeting".into(),
        }
    }

    fn egress_proof() -> CaptionEgressProof {
        CaptionEgressProof {
            proof_id: "11111111-2222-4333-8444-555555555555".into(),
            channel_id: "22222222-3333-4444-8555-666666666666".into(),
            sink_id: "33333333-4444-4555-8666-777777777777".into(),
            report_id: "44444444-5555-4666-8777-888888888888".into(),
            asset_id: "a".repeat(64),
            track_id: "55555555-6666-4777-8888-999999999999".into(),
            track_revision: 3,
            cues_sha256: "b".repeat(64),
            caption_policy_revision: 4,
            language: "en-US".into(),
            service_number: 1,
            candidate_version: "0.3.0".into(),
            source_commit: "0123456789abcdef0123456789abcdef01234567".into(),
            configuration_hash: "c".repeat(64),
            capture_started_asset_offset_ms: 1_000,
            capture_duration_ms: 20_000,
            capture_bytes: 1_000_000,
            capture_sha256: "d".repeat(64),
            artifact_relative_path: "caption-proofs/11111111-2222-4333-8444-555555555555.ts".into(),
            expected_cue_count: 2,
            decoded_cue_count: 2,
            expected_text_sha256: "e".repeat(64),
            decoded_text_sha256: "e".repeat(64),
            decoder_engine: "GStreamer 1.28.6".into(),
            decoder_method: "independent MPEG-TS CEA-708 decode-back".into(),
            outcome: CaptionProofOutcome::Verified,
            captured_at_unix_ms: 10_000,
        }
    }

    #[test]
    fn verified_egress_proof_requires_exact_bound_evidence() {
        assert_eq!(egress_proof().validate(), Ok(()));
    }

    #[test]
    fn verified_egress_proof_rejects_stale_or_unbound_authority() {
        let mut invalid = egress_proof();
        invalid.source_commit.clear();
        assert!(invalid.validate().is_err());

        let mut invalid = egress_proof();
        invalid.configuration_hash = "C".repeat(64);
        assert!(invalid.validate().is_err());

        let mut invalid = egress_proof();
        invalid.track_revision = 0;
        assert!(invalid.validate().is_err());

        let mut invalid = egress_proof();
        invalid.caption_policy_revision = 0;
        assert!(invalid.validate().is_err());

        let mut invalid = egress_proof();
        invalid.artifact_relative_path = "caption-proofs/other.ts".into();
        assert!(invalid.validate().is_err());
    }

    #[test]
    fn verified_egress_proof_rejects_missing_or_mismatched_decode_back() {
        let mut missing = egress_proof();
        missing.decoded_cue_count = 0;
        assert!(missing.validate().is_err());

        let mut unbounded = egress_proof();
        unbounded.capture_bytes = MAX_CAPTION_PROOF_CAPTURE_BYTES + 1;
        assert!(unbounded.validate().is_err());

        let mut mismatch_claimed_verified = egress_proof();
        mismatch_claimed_verified.decoded_text_sha256 = "f".repeat(64);
        assert!(mismatch_claimed_verified.validate().is_err());

        let mut honest_mismatch = mismatch_claimed_verified;
        honest_mismatch.outcome = CaptionProofOutcome::Mismatch;
        assert_eq!(honest_mismatch.validate(), Ok(()));

        let mut false_mismatch = egress_proof();
        false_mismatch.outcome = CaptionProofOutcome::Mismatch;
        assert!(false_mismatch.validate().is_err());
    }

    #[test]
    fn rejects_overlapping_asset_relative_cues() {
        let candidate = write(vec![
            AssetCaptionCue {
                cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
                start_ms: 1_000,
                duration_ms: 2_000,
                text: "First cue".into(),
            },
            AssetCaptionCue {
                cue_id: "19a07d83-940a-455d-868e-dade63a4e201".into(),
                start_ms: 2_999,
                duration_ms: 1_000,
                text: "Overlapping cue".into(),
            },
        ]);

        assert_eq!(
            candidate.validate(10_000),
            Err("caption cues must be ordered and non-overlapping".into())
        );
    }

    #[test]
    fn rejects_unbounded_or_noncanonical_track_authority() {
        let mut candidate = write(vec![cue()]);
        candidate.track_id = "NOT-A-CANONICAL-ID".into();

        assert_eq!(
            candidate.validate(10_000),
            Err("caption track identity must be a lowercase canonical UUID".into())
        );
    }

    #[test]
    fn rejects_invalid_caption_authority_metadata() {
        let mut invalid_asset = write(vec![cue()]);
        invalid_asset.asset_id = "A".repeat(64);
        assert!(invalid_asset.validate(10_000).is_err());

        let mut invalid_language = write(vec![cue()]);
        invalid_language.language = "English (United States)".into();
        assert!(invalid_language.validate(10_000).is_err());

        let mut invalid_service = write(vec![cue()]);
        invalid_service.service_number = 64;
        assert!(invalid_service.validate(10_000).is_err());

        let mut invalid_label = write(vec![cue()]);
        invalid_label.label = " label with outer whitespace ".into();
        assert!(invalid_label.validate(10_000).is_err());

        let mut invalid_actor = write(vec![cue()]);
        invalid_actor.actor = "setup\nadmin".into();
        assert!(invalid_actor.validate(10_000).is_err());

        let mut invalid_time = write(vec![cue()]);
        invalid_time.occurred_at_unix_ms = -1;
        assert!(invalid_time.validate(10_000).is_err());

        let mut invalid_audit = write(vec![cue()]);
        invalid_audit.audit_id = "audit".into();
        assert!(invalid_audit.validate(10_000).is_err());
    }

    #[test]
    fn rejects_invalid_cue_content_identity_and_asset_bounds() {
        assert!(write(Vec::new()).validate(10_000).is_err());

        let mut invalid_id = write(vec![cue()]);
        invalid_id.cues[0].cue_id = "cue-one".into();
        assert!(invalid_id.validate(10_000).is_err());

        let mut zero_duration = write(vec![cue()]);
        zero_duration.cues[0].duration_ms = 0;
        assert!(zero_duration.validate(10_000).is_err());

        let mut unbounded_duration = write(vec![cue()]);
        unbounded_duration.cues[0].duration_ms = 60_001;
        assert!(unbounded_duration.validate(100_000).is_err());

        let mut out_of_bounds = write(vec![cue()]);
        out_of_bounds.cues[0].start_ms = 9_000;
        assert!(out_of_bounds.validate(10_000).is_err());

        let mut blank_text = write(vec![cue()]);
        blank_text.cues[0].text = "   ".into();
        assert!(blank_text.validate(10_000).is_err());

        let mut control_text = write(vec![cue()]);
        control_text.cues[0].text = "bad\tcaption".into();
        assert!(control_text.validate(10_000).is_err());

        let duplicate = write(vec![
            cue(),
            AssetCaptionCue {
                cue_id: cue().cue_id,
                start_ms: 4_000,
                duration_ms: 1_000,
                text: "Duplicate identity".into(),
            },
        ]);
        assert!(duplicate.validate(10_000).is_err());
    }

    #[test]
    fn accepts_bounded_multiline_asset_relative_cues() {
        let candidate = write(vec![AssetCaptionCue {
            cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
            start_ms: 0,
            duration_ms: 10_000,
            text: "Town Council\nRegular Meeting".into(),
        }]);

        assert_eq!(candidate.validate(10_000), Ok(()));
    }

    #[test]
    fn cue_digest_binds_order_timing_identity_and_text() {
        let original = write(vec![
            cue(),
            AssetCaptionCue {
                cue_id: "19a07d83-940a-455d-868e-dade63a4e201".into(),
                start_ms: 4_000,
                duration_ms: 1_000,
                text: "Second cue".into(),
            },
        ]);
        let mut changed = original.clone();
        changed.cues[1].text = "Changed cue".into();

        assert_eq!(original.cues_sha256().len(), 64);
        assert!(
            original
                .cues_sha256()
                .bytes()
                .all(|byte| { byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte) })
        );
        assert_ne!(original.cues_sha256(), changed.cues_sha256());
    }

    #[test]
    fn proof_text_digest_normalizes_transport_whitespace_but_binds_sequence() {
        let source = vec![
            "Town Council\nRegular Meeting".to_string(),
            "Agenda item 2".to_string(),
        ];
        let decoded = vec![
            "Town  Council Regular\r\nMeeting".to_string(),
            "Agenda item 2".to_string(),
        ];
        assert_eq!(
            canonical_caption_text_sha256(&source).unwrap(),
            canonical_caption_text_sha256(&decoded).unwrap()
        );
        let reversed = vec![decoded[1].clone(), decoded[0].clone()];
        assert_ne!(
            canonical_caption_text_sha256(&source).unwrap(),
            canonical_caption_text_sha256(&reversed).unwrap()
        );
        assert!(canonical_caption_text_sha256(&[]).is_err());
    }

    #[test]
    fn expected_proof_window_includes_active_and_future_but_not_boundary_cues() {
        let cues = vec![
            AssetCaptionCue {
                cue_id: "11111111-2222-4333-8444-555555555555".into(),
                start_ms: 1_000,
                duration_ms: 1_500,
                text: "expired before capture".into(),
            },
            AssetCaptionCue {
                cue_id: "22222222-3333-4444-8555-666666666666".into(),
                start_ms: 2_500,
                duration_ms: 2_000,
                text: "active at capture".into(),
            },
            AssetCaptionCue {
                cue_id: "33333333-4444-4555-8666-777777777777".into(),
                start_ms: 5_000,
                duration_ms: 1_000,
                text: "future inside capture".into(),
            },
            AssetCaptionCue {
                cue_id: "44444444-5555-4666-8777-888888888888".into(),
                start_ms: 7_000,
                duration_ms: 1_000,
                text: "starts exactly after capture".into(),
            },
        ];
        let track = AssetCaptionTrack {
            track_id: "55555555-6666-4777-8888-999999999999".into(),
            asset_id: "a".repeat(64),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::External,
            label: "Reviewed English".into(),
            cues_sha256: cues_sha256(&cues),
            cues,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            updated_by: "setup-admin".into(),
        };
        let evidence = expected_caption_evidence_for_window(&track, 8_000, 3_000, 4_000).unwrap();
        assert_eq!(evidence.cue_count, 2);
        assert_eq!(
            evidence.text_sha256,
            canonical_caption_text_sha256(&[
                "active at capture".into(),
                "future inside capture".into()
            ])
            .unwrap()
        );
        assert!(expected_caption_evidence_for_window(&track, 8_000, 7_001, 1_000).is_err());
    }
}
