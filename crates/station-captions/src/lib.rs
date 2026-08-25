use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_CAPTION_CUES_PER_TRACK: usize = 100_000;
pub const MAX_CAPTION_CUE_DURATION_MS: u64 = 60_000;
pub const MAX_CAPTION_CUE_TEXT_BYTES: usize = 2_048;

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
}
