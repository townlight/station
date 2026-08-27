//! Pure, fail-closed publishing authority contracts.
//!
//! Provider calls, persistence, and secret storage are deliberately outside this crate.

use serde::{Deserialize, Serialize};

const MAX_TEXT: usize = 200;
const MAX_DETAIL: usize = 1_000;
const MAX_TARGETS: usize = 64;
const MAX_EVIDENCE: usize = 128;
const MAX_TEST_WINDOW_MS: i64 = 86_400_000;
const MAX_MEDIA_BYTES: u64 = 1_099_511_627_776;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishTargetKind {
    Portal,
    InternetArchive,
    LocalNas,
    YouTubeLive,
    Syndication,
    Podcast,
}

impl PublishTargetKind {
    pub const fn required_tier(self) -> Option<RequiredPublicationTier> {
        match self {
            Self::Portal => Some(RequiredPublicationTier::CanonicalPortal),
            Self::InternetArchive | Self::LocalNas => Some(RequiredPublicationTier::Permanence),
            Self::YouTubeLive => Some(RequiredPublicationTier::ReachCapacity),
            Self::Syndication | Self::Podcast => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RequiredPublicationTier {
    CanonicalPortal,
    Permanence,
    ReachCapacity,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishCandidateIdentity {
    pub version: String,
    pub source_commit: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishTargetAccount {
    pub account_id: String,
    pub kind: PublishTargetKind,
    pub label: String,
    pub enabled: bool,
    pub credential_configured: bool,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishTargetProfile {
    pub target_id: String,
    pub account_id: String,
    pub kind: PublishTargetKind,
    pub label: String,
    pub enabled: bool,
    pub configuration_hash: String,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionTestOutcome {
    Passed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishConnectionTestEvidence {
    pub evidence_id: String,
    pub target_id: String,
    pub account_revision: u64,
    pub target_revision: u64,
    pub configuration_hash: String,
    pub candidate: PublishCandidateIdentity,
    pub outcome: ConnectionTestOutcome,
    pub tested_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum PublishExecutionTargetConfig {
    Portal {
        endpoint: String,
    },
    InternetArchive {
        item_identifier: String,
    },
    LocalNas {
        managed_root: String,
    },
    YouTubeLive {
        channel_id: String,
        broadcast_id: String,
        stream_id: String,
        privacy_status: YouTubePrivacyStatus,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum YouTubePrivacyStatus {
    Public,
    Unlisted,
}

impl PublishExecutionTargetConfig {
    pub fn kind(&self) -> PublishTargetKind {
        match self {
            Self::Portal { .. } => PublishTargetKind::Portal,
            Self::InternetArchive { .. } => PublishTargetKind::InternetArchive,
            Self::LocalNas { .. } => PublishTargetKind::LocalNas,
            Self::YouTubeLive { .. } => PublishTargetKind::YouTubeLive,
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::Portal { endpoint } => validate_https_endpoint(endpoint),
            Self::InternetArchive { item_identifier } => {
                validate_slug(item_identifier, 100, "Internet Archive item identifier")
            }
            Self::LocalNas { managed_root } => {
                validate_visible(managed_root, 500, "managed NAS root")?;
                if !managed_root.starts_with("\\\\")
                    || managed_root.contains("..")
                    || managed_root.contains('/')
                {
                    return Err(
                        "managed NAS root must be an absolute UNC path without traversal".into(),
                    );
                }
                Ok(())
            }
            Self::YouTubeLive {
                channel_id,
                broadcast_id,
                stream_id,
                ..
            } => {
                validate_slug(channel_id, 100, "YouTube channel identity")?;
                validate_slug(broadcast_id, 100, "YouTube broadcast identity")?;
                validate_slug(stream_id, 100, "YouTube stream identity")
            }
        }
    }
}

/// Durable provider truth advances one explicit stage at a time. A later stage
/// never implies audience reach, playback, or retention beyond the provider's
/// exact acknowledgement.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishExecutionStage {
    Attempted,
    Uploaded,
    Delivered,
    Published,
    ExternallyVerified,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishExecutionReceipt {
    pub receipt_id: String,
    pub job_id: String,
    pub account_id: String,
    pub account_revision: u64,
    pub target_id: String,
    pub target_revision: u64,
    pub target_kind: PublishTargetKind,
    pub configuration_hash: String,
    pub credential_version: u64,
    pub candidate: PublishCandidateIdentity,
    pub media_sha256: String,
    pub media_bytes: u64,
    pub stage: PublishExecutionStage,
    /// Digest of the bounded provider acknowledgement or verified destination
    /// bytes. Raw provider response bodies never enter durable authority.
    pub delivery_identity_sha256: String,
    pub occurred_at_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub detail: String,
}

impl PublishExecutionReceipt {
    pub fn validate(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.receipt_id, "publish receipt identity"),
            (&self.job_id, "publish job identity"),
            (&self.account_id, "publish account identity"),
            (&self.target_id, "publish target identity"),
        ] {
            validate_uuid(value, field)?;
        }
        if self.account_revision == 0
            || self.target_revision == 0
            || self.credential_version == 0
            || !is_sha256(&self.configuration_hash)
            || !is_sha256(&self.media_sha256)
            || !is_sha256(&self.delivery_identity_sha256)
            || self.media_bytes == 0
            || self.media_bytes > MAX_MEDIA_BYTES
            || self.occurred_at_unix_ms < 0
            || self.recorded_at_unix_ms < self.occurred_at_unix_ms
        {
            return Err("publish execution receipt binding or evidence is invalid".into());
        }
        self.candidate.validate()?;
        validate_visible(&self.detail, MAX_DETAIL, "publish execution detail")
    }

    pub fn satisfies_required_delivery(&self) -> bool {
        let minimum = match self.target_kind {
            PublishTargetKind::Portal | PublishTargetKind::YouTubeLive => {
                PublishExecutionStage::Published
            }
            PublishTargetKind::InternetArchive | PublishTargetKind::LocalNas => {
                PublishExecutionStage::Delivered
            }
            PublishTargetKind::Syndication | PublishTargetKind::Podcast => return false,
        };
        self.stage >= minimum
    }
}

pub fn validate_receipt_successor(
    prior: &PublishExecutionReceipt,
    next: &PublishExecutionReceipt,
) -> Result<(), String> {
    prior.validate()?;
    next.validate()?;
    if prior.job_id != next.job_id
        || prior.account_id != next.account_id
        || prior.account_revision != next.account_revision
        || prior.target_id != next.target_id
        || prior.target_revision != next.target_revision
        || prior.target_kind != next.target_kind
        || prior.configuration_hash != next.configuration_hash
        || prior.credential_version != next.credential_version
        || prior.candidate != next.candidate
        || prior.media_sha256 != next.media_sha256
        || prior.media_bytes != next.media_bytes
        || next.stage <= prior.stage
        || next.occurred_at_unix_ms < prior.occurred_at_unix_ms
    {
        return Err("publish execution receipt does not advance the exact job binding".into());
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishTargetStatus {
    Disabled,
    CredentialUnavailable,
    NotRun,
    Passed,
    Failed,
    StaleConfiguration,
    StaleCandidate,
    Expired,
    InvalidEvidence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RequiredTierStatus {
    Passed,
    Blocked,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublishTargetStatusView {
    pub target_id: String,
    pub kind: PublishTargetKind,
    pub required_tier: Option<RequiredPublicationTier>,
    pub status: PublishTargetStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicationReadiness {
    pub targets: Vec<PublishTargetStatusView>,
    pub canonical_portal: RequiredTierStatus,
    pub permanence: RequiredTierStatus,
    pub reach_capacity: RequiredTierStatus,
}

impl PublishCandidateIdentity {
    pub fn validate(&self) -> Result<(), String> {
        validate_visible(&self.version, MAX_TEXT, "publish candidate version")?;
        if !is_source_commit(&self.source_commit) {
            return Err(
                "publish candidate source commit must be a 40-character lowercase hash".into(),
            );
        }
        Ok(())
    }
}

impl PublishTargetAccount {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.account_id, "publish target account identity")?;
        validate_visible(&self.label, MAX_TEXT, "publish target account label")?;
        if self.revision == 0 {
            return Err("publish target account revision must be nonzero".into());
        }
        Ok(())
    }
}

impl PublishTargetProfile {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.target_id, "publish target identity")?;
        validate_uuid(&self.account_id, "publish target account identity")?;
        validate_visible(&self.label, MAX_TEXT, "publish target label")?;
        if self.revision == 0 || !is_sha256(&self.configuration_hash) {
            return Err("publish target revision or configuration hash is invalid".into());
        }
        Ok(())
    }
}

impl PublishConnectionTestEvidence {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.evidence_id, "publish connection evidence identity")?;
        validate_uuid(
            &self.target_id,
            "publish connection evidence target identity",
        )?;
        if self.account_revision == 0
            || self.target_revision == 0
            || !is_sha256(&self.configuration_hash)
            || self.tested_at_unix_ms < 0
            || self.expires_at_unix_ms <= self.tested_at_unix_ms
            || self.expires_at_unix_ms - self.tested_at_unix_ms > MAX_TEST_WINDOW_MS
        {
            return Err("publish connection evidence is invalid or unbounded".into());
        }
        self.candidate.validate()?;
        validate_visible(
            &self.detail,
            MAX_DETAIL,
            "publish connection evidence detail",
        )
    }
}

pub fn derive_publish_target_status(
    account: &PublishTargetAccount,
    target: &PublishTargetProfile,
    evidence: Option<&PublishConnectionTestEvidence>,
    current_candidate: &PublishCandidateIdentity,
    now_unix_ms: i64,
) -> PublishTargetStatus {
    if now_unix_ms < 0
        || current_candidate.validate().is_err()
        || account.validate().is_err()
        || target.validate().is_err()
        || account.account_id != target.account_id
        || account.kind != target.kind
    {
        return PublishTargetStatus::InvalidEvidence;
    }
    if !account.enabled || !target.enabled {
        return PublishTargetStatus::Disabled;
    }
    if !account.credential_configured {
        return PublishTargetStatus::CredentialUnavailable;
    }
    let Some(evidence) = evidence else {
        return PublishTargetStatus::NotRun;
    };
    if evidence.validate().is_err() || evidence.target_id != target.target_id {
        return PublishTargetStatus::InvalidEvidence;
    }
    if evidence.tested_at_unix_ms > now_unix_ms {
        return PublishTargetStatus::InvalidEvidence;
    }
    if evidence.account_revision != account.revision
        || evidence.target_revision != target.revision
        || evidence.configuration_hash != target.configuration_hash
    {
        return PublishTargetStatus::StaleConfiguration;
    }
    if evidence.candidate != *current_candidate {
        return PublishTargetStatus::StaleCandidate;
    }
    if evidence.expires_at_unix_ms <= now_unix_ms {
        return PublishTargetStatus::Expired;
    }
    match evidence.outcome {
        ConnectionTestOutcome::Passed => PublishTargetStatus::Passed,
        ConnectionTestOutcome::Failed => PublishTargetStatus::Failed,
    }
}

pub fn derive_publication_readiness(
    accounts: &[PublishTargetAccount],
    targets: &[PublishTargetProfile],
    evidence: &[PublishConnectionTestEvidence],
    current_candidate: &PublishCandidateIdentity,
    now_unix_ms: i64,
) -> Result<PublicationReadiness, String> {
    if now_unix_ms < 0 {
        return Err("publishing readiness time must be nonnegative".into());
    }
    current_candidate.validate()?;
    if accounts.len() > MAX_TARGETS || targets.len() > MAX_TARGETS || evidence.len() > MAX_EVIDENCE
    {
        return Err(
            "publishing authority exceeds bounded account, target, or evidence counts".into(),
        );
    }
    validate_unique_accounts(accounts)?;
    validate_unique_targets(targets)?;
    validate_unique_evidence(evidence)?;
    let mut views = Vec::with_capacity(targets.len());
    for target in targets {
        let status = accounts
            .iter()
            .find(|account| account.account_id == target.account_id)
            .map(|account| {
                derive_publish_target_status(
                    account,
                    target,
                    evidence
                        .iter()
                        .find(|item| item.target_id == target.target_id),
                    current_candidate,
                    now_unix_ms,
                )
            })
            .unwrap_or(PublishTargetStatus::InvalidEvidence);
        views.push(PublishTargetStatusView {
            target_id: target.target_id.clone(),
            kind: target.kind,
            required_tier: target.kind.required_tier(),
            status,
        });
    }
    Ok(PublicationReadiness {
        canonical_portal: required_kind_status(&views, PublishTargetKind::Portal),
        permanence: permanence_status(&views),
        reach_capacity: required_kind_status(&views, PublishTargetKind::YouTubeLive),
        targets: views,
    })
}

fn required_kind_status(
    views: &[PublishTargetStatusView],
    kind: PublishTargetKind,
) -> RequiredTierStatus {
    let matching = views
        .iter()
        .filter(|view| view.kind == kind)
        .collect::<Vec<_>>();
    if matching.is_empty()
        || matching
            .iter()
            .any(|view| view.status == PublishTargetStatus::Disabled)
    {
        return RequiredTierStatus::Blocked;
    }
    if matching
        .iter()
        .all(|view| view.status == PublishTargetStatus::Passed)
    {
        RequiredTierStatus::Passed
    } else if matching.iter().any(|view| is_blocking_status(view.status)) {
        RequiredTierStatus::Blocked
    } else {
        RequiredTierStatus::Unknown
    }
}

fn permanence_status(views: &[PublishTargetStatusView]) -> RequiredTierStatus {
    let archive = required_kind_status(views, PublishTargetKind::InternetArchive);
    let nas = required_kind_status(views, PublishTargetKind::LocalNas);
    if archive == RequiredTierStatus::Passed && nas == RequiredTierStatus::Passed {
        RequiredTierStatus::Passed
    } else if archive == RequiredTierStatus::Blocked || nas == RequiredTierStatus::Blocked {
        RequiredTierStatus::Blocked
    } else {
        RequiredTierStatus::Unknown
    }
}

fn is_blocking_status(status: PublishTargetStatus) -> bool {
    matches!(
        status,
        PublishTargetStatus::Disabled
            | PublishTargetStatus::CredentialUnavailable
            | PublishTargetStatus::Failed
            | PublishTargetStatus::StaleConfiguration
            | PublishTargetStatus::StaleCandidate
            | PublishTargetStatus::Expired
            | PublishTargetStatus::InvalidEvidence
    )
}

fn validate_unique_accounts(accounts: &[PublishTargetAccount]) -> Result<(), String> {
    for account in accounts {
        account.validate()?;
    }
    if has_duplicate_by(accounts, |account| account.account_id.as_str()) {
        return Err("publish target account identities must be unique".into());
    }
    Ok(())
}

fn validate_unique_targets(targets: &[PublishTargetProfile]) -> Result<(), String> {
    for target in targets {
        target.validate()?;
    }
    if has_duplicate_by(targets, |target| target.target_id.as_str()) {
        return Err("publish target identities must be unique".into());
    }
    Ok(())
}

fn validate_unique_evidence(evidence: &[PublishConnectionTestEvidence]) -> Result<(), String> {
    for item in evidence {
        item.validate()?;
    }
    if has_duplicate_by(evidence, |item| item.target_id.as_str()) {
        return Err("publish connection evidence must be unique per target snapshot".into());
    }
    if has_duplicate_by(evidence, |item| item.evidence_id.as_str()) {
        return Err("publish connection evidence identities must be unique".into());
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
        return Err(format!("{field} must be a canonical UUID"));
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

fn validate_https_endpoint(value: &str) -> Result<(), String> {
    validate_visible(value, 500, "publishing HTTPS endpoint")?;
    let Some(authority) = value.strip_prefix("https://") else {
        return Err("publishing provider endpoint must use HTTPS".into());
    };
    if authority.is_empty()
        || authority.contains('@')
        || authority.contains('?')
        || authority.contains('#')
        || authority.ends_with('/')
    {
        return Err("publishing provider endpoint contains unsafe authority or URL data".into());
    }
    Ok(())
}

fn validate_slug(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    validate_visible(value, maximum, field)?;
    if value
        .bytes()
        .any(|byte| !(byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_')))
    {
        return Err(format!("{field} contains unsupported characters"));
    }
    Ok(())
}
