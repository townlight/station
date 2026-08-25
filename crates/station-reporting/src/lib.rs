//! Pure, fail-closed as-run projection, reporting, EPG, and export contracts.
//!
//! Typed facts are consumed from a future worker-journal reader. This crate neither
//! reads nor alters that journal, renders an export, or delivers one.

use std::collections::{BTreeMap, HashSet};

use serde::{Deserialize, Serialize};

pub const MAX_AS_RUN_FACTS: usize = 100_000;
pub const MAX_REPORT_ROWS: usize = 10_000;
pub const MAX_EXPORT_ROWS: usize = 100_000;
pub const MAX_EXPORT_RECEIPTS: usize = 100_000;
pub const MAX_TEXT_BYTES: usize = 200;
pub const MAX_WINDOW_MS: i64 = 366 * 86_400_000;

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

/// A typed, immutable fact expected from the worker journal projection boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunFact {
    pub event_id: String,
    pub channel_id: String,
    pub sequence: u64,
    pub content_id: String,
    pub franchise_id: String,
    pub category: ContentCategory,
    pub started_at_unix_ms: i64,
    pub ended_at_unix_ms: i64,
    pub provenance_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AsRunProjection {
    pub projection_id: String,
    pub channel_id: String,
    pub window: UtcWindow,
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
    pub source_sequence: u64,
    pub content_id: String,
    pub franchise_id: String,
    pub category: ContentCategory,
    pub starts_at_unix_ms: i64,
    pub ends_at_unix_ms: i64,
    pub title: String,
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

impl AsRunFact {
    pub fn validate(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.event_id, "as-run event identity"),
            (&self.channel_id, "as-run channel identity"),
            (&self.content_id, "as-run content identity"),
            (&self.franchise_id, "as-run franchise identity"),
        ] {
            validate_uuid(value, field)?;
        }
        if self.sequence == 0
            || self.started_at_unix_ms < 0
            || self.ended_at_unix_ms <= self.started_at_unix_ms
            || !is_sha256(&self.provenance_hash)
        {
            return Err("as-run fact sequence, UTC interval, or provenance hash is invalid".into());
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
        {
            return Err("as-run projection revision, fact count, or provenance count is invalid or unbounded".into());
        }
        let mut event_ids = HashSet::with_capacity(self.facts.len());
        let mut expected_sequence = self.facts.first().map(|fact| fact.sequence);
        let mut expected_start = self.window.starts_at_unix_ms;
        for (index, fact) in self.facts.iter().enumerate() {
            fact.validate()?;
            if fact.channel_id != self.channel_id
                || !event_ids.insert(fact.event_id.as_str())
                || expected_sequence != Some(fact.sequence)
                || fact.started_at_unix_ms != expected_start
                || self.source_fact_hashes[index] != fact.provenance_hash
            {
                return Err("as-run projection has a duplicate, sequence gap, UTC gap, or mismatched provenance".into());
            }
            expected_sequence = fact.sequence.checked_add(1);
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
        if self.recorded_at_unix_ms < 0 {
            return Err("export receipt time is invalid".into());
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
        validate_export_text(&row.title, "as-run export title")?;
        if row.source_sequence != fact.sequence
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
) -> Result<(), String> {
    proposed.validate()?;
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

    fn fact(
        sequence: u64,
        content_id: &str,
        category: ContentCategory,
        start: i64,
        end: i64,
        hash: char,
    ) -> AsRunFact {
        AsRunFact {
            event_id: format!("{sequence:08x}-1111-4111-8111-111111111111"),
            channel_id: CHANNEL.into(),
            sequence,
            content_id: content_id.into(),
            franchise_id: FRANCHISE.into(),
            category,
            started_at_unix_ms: start,
            ended_at_unix_ms: end,
            provenance_hash: hash.to_string().repeat(64),
        }
    }
    fn projection() -> AsRunProjection {
        let first = fact(
            1,
            CONTENT_ONE,
            ContentCategory::Program,
            1_700_000_000_000,
            1_700_000_600_000,
            'a',
        );
        let second = fact(
            2,
            CONTENT_TWO,
            ContentCategory::PublicAffairs,
            1_700_000_600_000,
            1_700_001_200_000,
            'b',
        );
        AsRunProjection {
            projection_id: "55555555-5555-4555-8555-555555555555".into(),
            channel_id: CHANNEL.into(),
            window: UtcWindow {
                starts_at_unix_ms: first.started_at_unix_ms,
                ends_at_unix_ms: second.ended_at_unix_ms,
            },
            source_fact_hashes: vec![
                first.provenance_hash.clone(),
                second.provenance_hash.clone(),
            ],
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
        gap.facts[1].sequence = 3;
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
                source_sequence: fact.sequence,
                content_id: fact.content_id.clone(),
                franchise_id: fact.franchise_id.clone(),
                category: fact.category,
                starts_at_unix_ms: fact.started_at_unix_ms,
                ends_at_unix_ms: fact.ended_at_unix_ms,
                title: "Civic report".into(),
            })
            .collect();
        let export = AsRunExportContract {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            format: ExportFormat::XmlTv,
            report_id: report.report_id.clone(),
            projection_id: projection.projection_id.clone(),
            projection_revision: 1,
            window: projection.window.clone(),
            source_fact_hashes: projection.source_fact_hashes.clone(),
            rows,
        };
        assert!(validate_export_contract(&export, &report, &projection, &epg(1)).is_ok());
        let mut other_channel = epg(1);
        other_channel.channel_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_export_contract(&export, &report, &projection, &other_channel).is_err());
        let mut unsafe_export = export;
        unsafe_export.rows[0].title = "=FORMULA".into();
        assert!(validate_export_contract(&unsafe_export, &report, &projection, &epg(1)).is_err());
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
    }

    #[test]
    fn export_receipts_are_immutable_not_delivery_claims() {
        let receipt = ExportReceipt {
            receipt_id: "99999999-9999-4999-8999-999999999999".into(),
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            recorded_at_unix_ms: 1_700_000_000_000,
            outcome: ExportReceiptOutcome::AcceptedForFutureRendering,
            detail: "queued for a later renderer".into(),
        };
        assert!(validate_export_receipt_append(&[], &receipt).is_ok());
        assert!(validate_export_receipt_append(std::slice::from_ref(&receipt), &receipt).is_err());
    }
}
