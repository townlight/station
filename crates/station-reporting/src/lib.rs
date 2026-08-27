//! Pure, fail-closed as-run projection, reporting, EPG, and export contracts.
//!
//! Typed facts are consumed from a future worker-journal reader. This crate neither
//! reads nor alters that journal, renders an export, or delivers one.

use std::collections::{BTreeMap, HashSet};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Runtime projection adapter. It accepts only journal records that have already
/// been bound to catalog and schedule authority; it never manufactures airings
/// from a browser or schedule intent.
pub mod runtime;

pub const MAX_AS_RUN_FACTS: usize = 100_000;
pub const MAX_REPORT_ROWS: usize = 10_000;
pub const MAX_EXPORT_ROWS: usize = 100_000;
pub const MAX_EXPORT_RECEIPTS: usize = 100_000;
pub const MAX_TEXT_BYTES: usize = 200;
pub const MAX_WINDOW_MS: i64 = 366 * 86_400_000;
pub const FACT_DIGEST_VERSION: u16 = 1;
pub const JOURNAL_EVIDENCE_DIGEST_VERSION: u16 = 1;
pub const EXPORT_DIGEST_VERSION: u16 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContentCategory {
    Program,
    News,
    PublicAffairs,
    Underwriting,
    Interstitial,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UtcWindow {
    pub starts_at_unix_ms: i64,
    pub ends_at_unix_ms: i64,
}

/// A future authenticated adapter's immutable binding to exact worker-journal evidence.
/// The current worker journal does not yet provide the timing/content enrichment required
/// to construct this value.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthenticatedJournalEvidenceBinding {
    pub evidence_id: String,
    pub journal_id: String,
    pub journal_record_sequence_start: u64,
    pub journal_record_sequence_end: u64,
    pub journal_record_hash: String,
    pub worker_candidate_hash: String,
    pub projected_fact_digest: String,
    pub authenticator_id: String,
    pub authenticated_at_unix_ms: i64,
    /// Digest of the future adapter's authentication proof; verification occurs at
    /// that trusted input boundary, not in this pure projection crate.
    pub authentication_proof_hash: String,
    pub digest_version: u16,
    pub binding_digest: String,
}

/// A typed, immutable future projection fact. Its digest is derived from every field
/// except the digest itself and is bound to separately authenticated journal evidence.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunFact {
    pub event_id: String,
    pub channel_id: String,
    pub projection_sequence: u64,
    pub content_id: String,
    pub franchise_id: String,
    pub category: ContentCategory,
    pub started_at_unix_ms: i64,
    pub ended_at_unix_ms: i64,
    pub journal_evidence_id: String,
    pub journal_evidence_digest: String,
    pub digest_version: u16,
    pub fact_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunProjection {
    pub projection_id: String,
    pub channel_id: String,
    pub window: UtcWindow,
    pub authenticated_journal_evidence: Vec<AuthenticatedJournalEvidenceBinding>,
    pub facts: Vec<AsRunFact>,
    /// Exact ordered source fact hashes; a downstream report cannot substitute facts.
    pub source_fact_hashes: Vec<String>,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HoursByFranchiseCategory {
    pub franchise_id: String,
    pub category: ContentCategory,
    pub duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FranchiseCategoryReport {
    pub report_id: String,
    pub projection_id: String,
    pub projection_revision: u64,
    pub window: UtcWindow,
    pub source_fact_hashes: Vec<String>,
    pub hours: Vec<HoursByFranchiseCategory>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportFormat {
    XList,
    XmlTv,
    Csv,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunExportRow {
    pub projection_sequence: u64,
    pub content_id: String,
    pub franchise_id: String,
    pub category: ContentCategory,
    pub starts_at_unix_ms: i64,
    pub ends_at_unix_ms: i64,
}

/// A renderer input contract for X-List, XMLTV, or CSV. It is not rendered bytes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunExportContract {
    pub export_id: String,
    pub format: ExportFormat,
    pub report_id: String,
    pub projection_id: String,
    pub projection_revision: u64,
    pub window: UtcWindow,
    pub source_fact_hashes: Vec<String>,
    pub rows: Vec<AsRunExportRow>,
    pub digest_version: u16,
    pub export_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EpgConfiguration {
    pub configuration_id: String,
    pub channel_id: String,
    pub service_name: String,
    pub xmltv_enabled: bool,
    pub x_list_enabled: bool,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportReceiptOutcome {
    AcceptedForFutureRendering,
    RejectedBeforeRendering,
}

/// Append-only authority record, never an export-rendering or delivery outcome.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExportReceipt {
    pub receipt_id: String,
    pub export_id: String,
    pub export_digest: String,
    pub report_id: String,
    pub projection_id: String,
    pub projection_revision: u64,
    pub source_fact_hashes: Vec<String>,
    pub recorded_at_unix_ms: i64,
    pub outcome: ExportReceiptOutcome,
    pub detail: String,
}

impl UtcWindow {
    pub fn validate(&self) -> Result<(), String> {
        if self.starts_at_unix_ms < 0
            || self.ends_at_unix_ms <= self.starts_at_unix_ms
            || self.ends_at_unix_ms - self.starts_at_unix_ms > MAX_WINDOW_MS
        {
            return Err("UTC reporting window is ambiguous, invalid, or unbounded".into());
        }
        Ok(())
    }
}

impl AuthenticatedJournalEvidenceBinding {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.evidence_id, "journal evidence identity")?;
        validate_uuid(&self.journal_id, "worker journal identity")?;
        validate_uuid(
            &self.authenticator_id,
            "journal evidence authenticator identity",
        )?;
        if self.journal_record_sequence_start == 0
            || self.journal_record_sequence_end < self.journal_record_sequence_start
            || !is_sha256(&self.journal_record_hash)
            || !is_sha256(&self.worker_candidate_hash)
            || !is_sha256(&self.projected_fact_digest)
            || self.authenticated_at_unix_ms < 0
            || !is_sha256(&self.authentication_proof_hash)
            || self.digest_version != JOURNAL_EVIDENCE_DIGEST_VERSION
            || self.binding_digest != canonical_journal_evidence_digest(self)
        {
            return Err(
                "journal evidence range, hashes, authentication, or canonical digest is invalid"
                    .into(),
            );
        }
        Ok(())
    }
}

impl AsRunFact {
    pub fn validate(&self, evidence: &AuthenticatedJournalEvidenceBinding) -> Result<(), String> {
        for (value, field) in [
            (&self.event_id, "as-run event identity"),
            (&self.channel_id, "as-run channel identity"),
            (&self.content_id, "as-run content identity"),
            (&self.franchise_id, "as-run franchise identity"),
            (
                &self.journal_evidence_id,
                "as-run journal evidence identity",
            ),
        ] {
            validate_uuid(value, field)?;
        }
        evidence.validate()?;
        if self.projection_sequence == 0
            || self.started_at_unix_ms < 0
            || self.ended_at_unix_ms <= self.started_at_unix_ms
            || self.journal_evidence_id != evidence.evidence_id
            || self.journal_evidence_digest != evidence.binding_digest
            || self.digest_version != FACT_DIGEST_VERSION
            || self.fact_digest != canonical_fact_digest(self)
            || self.fact_digest != evidence.projected_fact_digest
        {
            return Err("as-run fact sequence, UTC interval, journal binding, or canonical digest is invalid".into());
        }
        Ok(())
    }

    fn duration_ms(&self) -> u64 {
        (self.ended_at_unix_ms - self.started_at_unix_ms) as u64
    }
}

impl AsRunProjection {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.projection_id, "as-run projection identity")?;
        validate_uuid(&self.channel_id, "as-run projection channel identity")?;
        self.window.validate()?;
        if self.revision == 0
            || self.facts.is_empty()
            || self.facts.len() > MAX_AS_RUN_FACTS
            || self.source_fact_hashes.len() != self.facts.len()
            || self.authenticated_journal_evidence.len() != self.facts.len()
        {
            return Err("as-run projection revision, fact count, or provenance count is invalid or unbounded".into());
        }
        let mut event_ids = HashSet::with_capacity(self.facts.len());
        let mut evidence_ids = HashSet::with_capacity(self.authenticated_journal_evidence.len());
        let mut evidence_by_id =
            std::collections::HashMap::with_capacity(self.authenticated_journal_evidence.len());
        for evidence in &self.authenticated_journal_evidence {
            evidence.validate()?;
            if !evidence_ids.insert(evidence.evidence_id.as_str()) {
                return Err("as-run projection journal evidence identities must be unique".into());
            }
            evidence_by_id.insert(evidence.evidence_id.as_str(), evidence);
        }
        let mut expected_sequence = self.facts.first().map(|fact| fact.projection_sequence);
        let mut expected_start = self.window.starts_at_unix_ms;
        for (index, fact) in self.facts.iter().enumerate() {
            let evidence = evidence_by_id
                .get(fact.journal_evidence_id.as_str())
                .ok_or("as-run fact has no authenticated journal evidence binding")?;
            fact.validate(evidence)?;
            if fact.channel_id != self.channel_id
                || !event_ids.insert(fact.event_id.as_str())
                || expected_sequence != Some(fact.projection_sequence)
                || fact.started_at_unix_ms != expected_start
                || self.source_fact_hashes[index] != fact.fact_digest
            {
                return Err("as-run projection has a duplicate, sequence gap, UTC gap, or mismatched provenance".into());
            }
            expected_sequence = fact.projection_sequence.checked_add(1);
            expected_start = fact.ended_at_unix_ms;
        }
        if expected_start != self.window.ends_at_unix_ms {
            return Err("as-run projection does not exactly cover its UTC window".into());
        }
        Ok(())
    }
}

impl EpgConfiguration {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.configuration_id, "EPG configuration identity")?;
        validate_uuid(&self.channel_id, "EPG configuration channel identity")?;
        validate_export_text(&self.service_name, "EPG service name")?;
        if self.revision == 0 || (!self.xmltv_enabled && !self.x_list_enabled) {
            return Err("EPG configuration revision or enabled export set is invalid".into());
        }
        Ok(())
    }
}

impl ExportReceipt {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.receipt_id, "export receipt identity")?;
        validate_uuid(&self.export_id, "export receipt export identity")?;
        validate_uuid(&self.report_id, "export receipt report identity")?;
        validate_uuid(&self.projection_id, "export receipt projection identity")?;
        if !is_sha256(&self.export_digest)
            || self.projection_revision == 0
            || self.source_fact_hashes.is_empty()
            || self.source_fact_hashes.len() > MAX_AS_RUN_FACTS
            || self.source_fact_hashes.iter().any(|hash| !is_sha256(hash))
            || self.recorded_at_unix_ms < 0
        {
            return Err(
                "export receipt binding, source hashes, revision, or time is invalid".into(),
            );
        }
        validate_export_text(&self.detail, "export receipt detail")
    }
}

pub fn aggregate_hours(
    projection: &AsRunProjection,
) -> Result<Vec<HoursByFranchiseCategory>, String> {
    projection.validate()?;
    let mut durations = BTreeMap::<(String, ContentCategory), u64>::new();
    for fact in &projection.facts {
        let entry = durations
            .entry((fact.franchise_id.clone(), fact.category))
            .or_default();
        *entry = entry
            .checked_add(fact.duration_ms())
            .ok_or("as-run hours aggregation overflow")?;
    }
    Ok(durations
        .into_iter()
        .map(
            |((franchise_id, category), duration_ms)| HoursByFranchiseCategory {
                franchise_id,
                category,
                duration_ms,
            },
        )
        .collect())
}

pub fn validate_franchise_category_report(
    report: &FranchiseCategoryReport,
    projection: &AsRunProjection,
) -> Result<(), String> {
    validate_uuid(&report.report_id, "franchise report identity")?;
    projection.validate()?;
    report.window.validate()?;
    if report.projection_id != projection.projection_id
        || report.projection_revision != projection.revision
        || report.window != projection.window
        || report.source_fact_hashes != projection.source_fact_hashes
        || report.hours.len() > MAX_REPORT_ROWS
        || report.hours != aggregate_hours(projection)?
    {
        return Err(
            "franchise report has forged totals, stale projection, or invalid provenance".into(),
        );
    }
    Ok(())
}

pub fn validate_export_contract(
    export: &AsRunExportContract,
    report: &FranchiseCategoryReport,
    projection: &AsRunProjection,
    epg: &EpgConfiguration,
) -> Result<(), String> {
    validate_uuid(&export.export_id, "as-run export identity")?;
    validate_franchise_category_report(report, projection)?;
    epg.validate()?;
    if export.rows.is_empty()
        || export.rows.len() > MAX_EXPORT_ROWS
        || export.report_id != report.report_id
        || export.projection_id != projection.projection_id
        || export.projection_revision != projection.revision
        || export.window != projection.window
        || export.source_fact_hashes != projection.source_fact_hashes
        || export.digest_version != EXPORT_DIGEST_VERSION
        || export.export_digest != canonical_export_digest(export)
        || epg.channel_id != projection.channel_id
        || matches!(export.format, ExportFormat::XmlTv) && !epg.xmltv_enabled
        || matches!(export.format, ExportFormat::XList) && !epg.x_list_enabled
    {
        return Err(
            "as-run export has an unsupported format, stale report, or unbounded rows".into(),
        );
    }
    if export.rows.len() != projection.facts.len() {
        return Err("as-run export rows must exactly represent the source facts".into());
    }
    for (row, fact) in export.rows.iter().zip(&projection.facts) {
        if row.projection_sequence != fact.projection_sequence
            || row.content_id != fact.content_id
            || row.franchise_id != fact.franchise_id
            || row.category != fact.category
            || row.starts_at_unix_ms != fact.started_at_unix_ms
            || row.ends_at_unix_ms != fact.ended_at_unix_ms
        {
            return Err("as-run export row does not exactly bind a source fact".into());
        }
    }
    Ok(())
}

pub fn validate_epg_configuration_write(
    current: Option<&EpgConfiguration>,
    expected_revision: Option<u64>,
    proposed: &EpgConfiguration,
) -> Result<(), String> {
    proposed.validate()?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => {
            Err("EPG configuration creation requires revision one and no expected revision".into())
        }
        Some(current) => {
            current.validate()?;
            if expected_revision != Some(current.revision)
                || current.revision.checked_add(1) != Some(proposed.revision)
            {
                return Err(
                    "EPG configuration write has a stale or invalid compare-and-set revision"
                        .into(),
                );
            }
            if proposed.configuration_id != current.configuration_id
                || proposed.channel_id != current.channel_id
            {
                return Err("EPG configuration identity and channel cannot be rebound".into());
            }
            Ok(())
        }
    }
}

pub fn validate_export_receipt_append(
    existing: &[ExportReceipt],
    proposed: &ExportReceipt,
    authoritative_export: &AsRunExportContract,
    report: &FranchiseCategoryReport,
    projection: &AsRunProjection,
    epg: &EpgConfiguration,
) -> Result<(), String> {
    proposed.validate()?;
    validate_export_contract(authoritative_export, report, projection, epg)?;
    if proposed.export_id != authoritative_export.export_id
        || proposed.export_digest != authoritative_export.export_digest
        || proposed.report_id != authoritative_export.report_id
        || proposed.projection_id != authoritative_export.projection_id
        || proposed.projection_revision != authoritative_export.projection_revision
        || proposed.source_fact_hashes != authoritative_export.source_fact_hashes
    {
        return Err("export receipt must bind the exact authoritative validated export".into());
    }
    if existing.len() >= MAX_EXPORT_RECEIPTS {
        return Err("export receipt context exceeds its bounded record count".into());
    }
    let mut receipt_ids = HashSet::with_capacity(existing.len());
    let mut export_ids = HashSet::with_capacity(existing.len());
    for receipt in existing {
        receipt.validate()?;
        if !receipt_ids.insert(receipt.receipt_id.as_str())
            || !export_ids.insert(receipt.export_id.as_str())
        {
            return Err("existing export receipts are not immutable and unique".into());
        }
    }
    if receipt_ids.contains(proposed.receipt_id.as_str())
        || export_ids.contains(proposed.export_id.as_str())
    {
        return Err("export receipt identity and export may be recorded only once".into());
    }
    Ok(())
}

pub fn canonical_journal_evidence_digest(evidence: &AuthenticatedJournalEvidenceBinding) -> String {
    let mut canonical = CanonicalDigest::new("townlight.reporting.journal-evidence");
    canonical.u16(evidence.digest_version);
    canonical.text(&evidence.evidence_id);
    canonical.text(&evidence.journal_id);
    canonical.u64(evidence.journal_record_sequence_start);
    canonical.u64(evidence.journal_record_sequence_end);
    canonical.text(&evidence.journal_record_hash);
    canonical.text(&evidence.worker_candidate_hash);
    canonical.text(&evidence.projected_fact_digest);
    canonical.text(&evidence.authenticator_id);
    canonical.i64(evidence.authenticated_at_unix_ms);
    canonical.text(&evidence.authentication_proof_hash);
    canonical.finish()
}

pub fn canonical_fact_digest(fact: &AsRunFact) -> String {
    let mut canonical = CanonicalDigest::new("townlight.reporting.as-run-fact");
    canonical.u16(fact.digest_version);
    canonical.text(&fact.event_id);
    canonical.text(&fact.channel_id);
    canonical.u64(fact.projection_sequence);
    canonical.text(&fact.content_id);
    canonical.text(&fact.franchise_id);
    canonical.byte(category_code(fact.category));
    canonical.i64(fact.started_at_unix_ms);
    canonical.i64(fact.ended_at_unix_ms);
    canonical.text(&fact.journal_evidence_id);
    canonical.finish()
}

pub fn canonical_export_digest(export: &AsRunExportContract) -> String {
    let mut canonical = CanonicalDigest::new("townlight.reporting.export-contract");
    canonical.u16(export.digest_version);
    canonical.text(&export.export_id);
    canonical.byte(export_format_code(export.format));
    canonical.text(&export.report_id);
    canonical.text(&export.projection_id);
    canonical.u64(export.projection_revision);
    canonical.i64(export.window.starts_at_unix_ms);
    canonical.i64(export.window.ends_at_unix_ms);
    canonical.u64(export.source_fact_hashes.len() as u64);
    for hash in &export.source_fact_hashes {
        canonical.text(hash);
    }
    canonical.u64(export.rows.len() as u64);
    for row in &export.rows {
        canonical.u64(row.projection_sequence);
        canonical.text(&row.content_id);
        canonical.text(&row.franchise_id);
        canonical.byte(category_code(row.category));
        canonical.i64(row.starts_at_unix_ms);
        canonical.i64(row.ends_at_unix_ms);
    }
    canonical.finish()
}

struct CanonicalDigest(Sha256);

impl CanonicalDigest {
    fn new(domain: &str) -> Self {
        let mut value = Self(Sha256::new());
        value.text(domain);
        value
    }

    fn byte(&mut self, value: u8) {
        self.0.update([value]);
    }

    fn u16(&mut self, value: u16) {
        self.0.update(value.to_be_bytes());
    }

    fn u64(&mut self, value: u64) {
        self.0.update(value.to_be_bytes());
    }

    fn i64(&mut self, value: i64) {
        self.0.update(value.to_be_bytes());
    }

    fn text(&mut self, value: &str) {
        self.u64(value.len() as u64);
        self.0.update(value.as_bytes());
    }

    fn finish(self) -> String {
        let bytes = self.0.finalize();
        let mut value = String::with_capacity(64);
        for byte in bytes {
            use std::fmt::Write;
            let _ = write!(value, "{byte:02x}");
        }
        value
    }
}

fn category_code(category: ContentCategory) -> u8 {
    match category {
        ContentCategory::Program => 1,
        ContentCategory::News => 2,
        ContentCategory::PublicAffairs => 3,
        ContentCategory::Underwriting => 4,
        ContentCategory::Interstitial => 5,
    }
}

fn export_format_code(format: ExportFormat) -> u8 {
    match format {
        ExportFormat::XList => 1,
        ExportFormat::XmlTv => 2,
        ExportFormat::Csv => 3,
    }
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            !matches!(index, 8 | 13 | 18 | 23)
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_export_text(value: &str, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > MAX_TEXT_BYTES
        || value.chars().any(char::is_control)
        || value
            .chars()
            .next()
            .is_some_and(|character| matches!(character, '=' | '+' | '-' | '@'))
        || value
            .chars()
            .any(|character| matches!(character, '<' | '>' | '&' | '"' | '\'' | ',' | '\\'))
    {
        return Err(format!(
            "{field} is unsafe or unbounded for X-List/XMLTV/CSV export"
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

#[cfg(test)]
mod tests {
    use super::*;

    const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
    const FRANCHISE: &str = "22222222-2222-4222-8222-222222222222";
    const CONTENT_ONE: &str = "33333333-3333-4333-8333-333333333333";
    const CONTENT_TWO: &str = "44444444-4444-4444-8444-444444444444";

    fn evidence(sequence: u64, hash: char) -> AuthenticatedJournalEvidenceBinding {
        AuthenticatedJournalEvidenceBinding {
            evidence_id: format!("{sequence:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            journal_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab".into(),
            journal_record_sequence_start: sequence * 10,
            journal_record_sequence_end: sequence * 10 + 2,
            journal_record_hash: hash.to_string().repeat(64),
            worker_candidate_hash: "c".repeat(64),
            projected_fact_digest: String::new(),
            authenticator_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac".into(),
            authenticated_at_unix_ms: 1_700_000_000_000,
            authentication_proof_hash: "d".repeat(64),
            digest_version: JOURNAL_EVIDENCE_DIGEST_VERSION,
            binding_digest: String::new(),
        }
    }
    fn fact(
        projection_sequence: u64,
        content_id: &str,
        category: ContentCategory,
        start: i64,
        end: i64,
        evidence: &AuthenticatedJournalEvidenceBinding,
    ) -> AsRunFact {
        let mut fact = AsRunFact {
            event_id: format!("{projection_sequence:08x}-1111-4111-8111-111111111111"),
            channel_id: CHANNEL.into(),
            projection_sequence,
            content_id: content_id.into(),
            franchise_id: FRANCHISE.into(),
            category,
            started_at_unix_ms: start,
            ended_at_unix_ms: end,
            journal_evidence_id: evidence.evidence_id.clone(),
            journal_evidence_digest: evidence.binding_digest.clone(),
            digest_version: FACT_DIGEST_VERSION,
            fact_digest: String::new(),
        };
        fact.fact_digest = canonical_fact_digest(&fact);
        fact
    }
    fn projection() -> AsRunProjection {
        let mut first_evidence = evidence(1, 'a');
        let mut second_evidence = evidence(2, 'b');
        let mut first = fact(
            1,
            CONTENT_ONE,
            ContentCategory::Program,
            1_700_000_000_000,
            1_700_000_600_000,
            &first_evidence,
        );
        let mut second = fact(
            2,
            CONTENT_TWO,
            ContentCategory::PublicAffairs,
            1_700_000_600_000,
            1_700_001_200_000,
            &second_evidence,
        );
        first_evidence.projected_fact_digest = first.fact_digest.clone();
        first_evidence.binding_digest = canonical_journal_evidence_digest(&first_evidence);
        first.journal_evidence_digest = first_evidence.binding_digest.clone();
        second_evidence.projected_fact_digest = second.fact_digest.clone();
        second_evidence.binding_digest = canonical_journal_evidence_digest(&second_evidence);
        second.journal_evidence_digest = second_evidence.binding_digest.clone();
        AsRunProjection {
            projection_id: "55555555-5555-4555-8555-555555555555".into(),
            channel_id: CHANNEL.into(),
            window: UtcWindow {
                starts_at_unix_ms: first.started_at_unix_ms,
                ends_at_unix_ms: second.ended_at_unix_ms,
            },
            source_fact_hashes: vec![first.fact_digest.clone(), second.fact_digest.clone()],
            authenticated_journal_evidence: vec![first_evidence, second_evidence],
            facts: vec![first, second],
            revision: 1,
        }
    }
    fn epg(revision: u64) -> EpgConfiguration {
        EpgConfiguration {
            configuration_id: "66666666-6666-4666-8666-666666666666".into(),
            channel_id: CHANNEL.into(),
            service_name: "TownLight Civic".into(),
            xmltv_enabled: true,
            x_list_enabled: true,
            revision,
        }
    }

    #[test]
    fn projects_contiguous_utc_facts_and_derives_deterministic_hours_report() {
        let projection = projection();
        let report = FranchiseCategoryReport {
            report_id: "77777777-7777-4777-8777-777777777777".into(),
            projection_id: projection.projection_id.clone(),
            projection_revision: 1,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            hours: aggregate_hours(&projection).expect("valid projection"),
        };
        assert!(projection.validate().is_ok());
        assert!(validate_franchise_category_report(&report, &projection).is_ok());
    }

    #[test]
    fn rejects_sequence_gaps_duplicates_utc_gaps_and_forged_totals() {
        let mut gap = projection();
        gap.facts[1].projection_sequence = 3;
        assert!(gap.validate().is_err());
        let mut overlap = projection();
        overlap.facts[1].started_at_unix_ms -= 1;
        assert!(overlap.validate().is_err());
        let projection = projection();
        let report = FranchiseCategoryReport {
            report_id: "77777777-7777-4777-8777-777777777777".into(),
            projection_id: projection.projection_id.clone(),
            projection_revision: 1,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            hours: vec![],
        };
        assert!(validate_franchise_category_report(&report, &projection).is_err());
    }

    #[test]
    fn canonical_fact_and_journal_evidence_digests_reject_altered_fields_or_hashes() {
        let mut altered_fact = projection();
        altered_fact.facts[0].content_id = CONTENT_TWO.into();
        assert!(altered_fact.validate().is_err());

        let mut forged_fact_digest = projection();
        forged_fact_digest.facts[0].fact_digest = "f".repeat(64);
        forged_fact_digest.source_fact_hashes[0] = "f".repeat(64);
        assert!(forged_fact_digest.validate().is_err());

        let mut altered_evidence = projection();
        altered_evidence.authenticated_journal_evidence[0].journal_record_sequence_end += 1;
        assert!(altered_evidence.validate().is_err());

        let mut altered_authentication = projection();
        altered_authentication.authenticated_journal_evidence[0].authentication_proof_hash =
            "e".repeat(64);
        assert!(altered_authentication.validate().is_err());
    }

    #[test]
    fn validates_xlist_xmltv_csv_export_contracts_and_rejects_unsafe_fields() {
        let projection = projection();
        let report = FranchiseCategoryReport {
            report_id: "77777777-7777-4777-8777-777777777777".into(),
            projection_id: projection.projection_id.clone(),
            projection_revision: 1,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            hours: aggregate_hours(&projection).expect("valid projection"),
        };
        let rows = projection
            .facts
            .iter()
            .map(|fact| AsRunExportRow {
                projection_sequence: fact.projection_sequence,
                content_id: fact.content_id.clone(),
                franchise_id: fact.franchise_id.clone(),
                category: fact.category,
                starts_at_unix_ms: fact.started_at_unix_ms,
                ends_at_unix_ms: fact.ended_at_unix_ms,
            })
            .collect();
        let mut export = AsRunExportContract {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            format: ExportFormat::XmlTv,
            report_id: report.report_id.clone(),
            projection_id: projection.projection_id.clone(),
            projection_revision: 1,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            rows,
            digest_version: EXPORT_DIGEST_VERSION,
            export_digest: String::new(),
        };
        export.export_digest = canonical_export_digest(&export);
        assert!(validate_export_contract(&export, &report, &projection, &epg(1)).is_ok());
        let mut other_channel = epg(1);
        other_channel.channel_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_export_contract(&export, &report, &projection, &other_channel).is_err());
        let mut altered_export = export;
        altered_export.rows[0].ends_at_unix_ms -= 1;
        assert!(validate_export_contract(&altered_export, &report, &projection, &epg(1)).is_err());
    }

    #[test]
    fn epg_cas_rejects_stale_rebound_and_unknown_configuration() {
        assert!(validate_epg_configuration_write(None, None, &epg(1)).is_ok());
        assert!(validate_epg_configuration_write(Some(&epg(1)), Some(2), &epg(2)).is_err());
        let mut rebound = epg(2);
        rebound.channel_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_epg_configuration_write(Some(&epg(1)), Some(1), &rebound).is_err());
        assert!(
            serde_json::from_str::<EpgConfiguration>(
                r#"{"configuration_id":"66666666-6666-4666-8666-666666666666","unknown":true}"#
            )
            .is_err()
        );
        let mut unsafe_service = epg(1);
        unsafe_service.service_name = "=FORGED".into();
        assert!(unsafe_service.validate().is_err());
    }

    #[test]
    fn export_receipts_are_immutable_not_delivery_claims() {
        let projection = projection();
        let report = FranchiseCategoryReport {
            report_id: "77777777-7777-4777-8777-777777777777".into(),
            projection_id: projection.projection_id.clone(),
            projection_revision: projection.revision,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            hours: aggregate_hours(&projection).expect("valid projection"),
        };
        let rows = projection
            .facts
            .iter()
            .map(|fact| AsRunExportRow {
                projection_sequence: fact.projection_sequence,
                content_id: fact.content_id.clone(),
                franchise_id: fact.franchise_id.clone(),
                category: fact.category,
                starts_at_unix_ms: fact.started_at_unix_ms,
                ends_at_unix_ms: fact.ended_at_unix_ms,
            })
            .collect();
        let mut export = AsRunExportContract {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            format: ExportFormat::Csv,
            report_id: report.report_id.clone(),
            projection_id: projection.projection_id.clone(),
            projection_revision: projection.revision,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            rows,
            digest_version: EXPORT_DIGEST_VERSION,
            export_digest: String::new(),
        };
        export.export_digest = canonical_export_digest(&export);
        let receipt = ExportReceipt {
            receipt_id: "99999999-9999-4999-8999-999999999999".into(),
            export_id: export.export_id.clone(),
            export_digest: export.export_digest.clone(),
            report_id: report.report_id.clone(),
            projection_id: projection.projection_id.clone(),
            projection_revision: projection.revision,
            source_fact_hashes: projection.source_fact_hashes.clone(),
            recorded_at_unix_ms: 1_700_000_000_000,
            outcome: ExportReceiptOutcome::AcceptedForFutureRendering,
            detail: "queued for a later renderer".into(),
        };
        assert!(
            validate_export_receipt_append(&[], &receipt, &export, &report, &projection, &epg(1))
                .is_ok()
        );
        assert!(
            validate_export_receipt_append(
                std::slice::from_ref(&receipt),
                &receipt,
                &export,
                &report,
                &projection,
                &epg(1)
            )
            .is_err()
        );
        let mut mismatched = receipt.clone();
        mismatched.export_digest = "f".repeat(64);
        assert!(
            validate_export_receipt_append(
                &[],
                &mismatched,
                &export,
                &report,
                &projection,
                &epg(1)
            )
            .is_err()
        );
        let mut unknown_export = receipt;
        unknown_export.export_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into();
        assert!(
            validate_export_receipt_append(
                &[],
                &unknown_export,
                &export,
                &report,
                &projection,
                &epg(1)
            )
            .is_err()
        );
    }
}
