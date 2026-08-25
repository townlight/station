use jiff::{civil::DateTime, tz::TimeZone};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

const MAX_TERM_LEN: usize = 64;
const MAX_LABEL_LEN: usize = 80;
const MAX_QUERY_FILTERS: usize = 8;
const MAX_QUERY_LIMIT: usize = 128;
const MAX_WINDOW_MS: i64 = 60 * 86_400_000;
const MAX_SLOTS: usize = 64;
const MAX_EVIDENCE_RECEIPT_LIFETIME_MS: i64 = 300_000;
/// Version three binds each materialization to authenticated catalog and
/// as-run projections. Version two intents did not contain that evidence and
/// must not be silently reinterpreted as proved materializations.
const HASH_SCHEMA_VERSION: u16 = 3;
/// The rule/search/block authority contract itself remains the established v2
/// encoding. Keeping it stable also keeps occurrence identities stable.
const SOURCE_AUTHORITY_SCHEMA_VERSION: u16 = 2;
const OCCURRENCE_SCHEMA_VERSION: u16 = 2;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SavedSearch {
    pub search_id: String,
    pub channel_id: String,
    pub owner_operator_id: String,
    pub query: SavedSearchQuery,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MetadataFilter {
    pub field: String,
    pub value: MetadataValue,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum MetadataValue {
    Text(String),
    Number(i64),
    Boolean(bool),
    DateTimeUnixMs(i64),
    Choice(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SavedSearchOrder {
    TitleAscending,
    PublishedNewest,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SavedSearchQuery {
    pub all_of: Vec<MetadataFilter>,
    pub any_of: Vec<MetadataFilter>,
    pub order_by: SavedSearchOrder,
    pub limit: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AssetCandidate {
    pub asset_sha256: String,
    pub title: String,
    pub duration_ms: u64,
    pub ready: bool,
    pub published: bool,
    pub published_at_utc_ms: i64,
    pub indexed_custom_metadata: BTreeMap<String, MetadataValue>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PriorAiring {
    pub asset_sha256: String,
    pub aired_at_utc_ms: i64,
}

/// The two projection kinds which must be independently authenticated before
/// a materialization may regard an empty result as authoritative.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceEvidenceKind {
    CandidateProjection,
    PriorAirings,
}

/// A receipt produced by the authenticated catalog/as-run boundary.
///
/// This crate deliberately does not implement key management or signature
/// verification. The caller supplies a verifier for the receipt, while this
/// typed contract binds that verified receipt to one exact materialization
/// scope and one exact canonical payload digest. Receipts are reusable immutable
/// snapshot attestations only for that scope during their bounded five-minute
/// validity period; they are not one-shot bearer tokens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceEvidenceReceipt {
    pub issuer_id: String,
    pub receipt_id: String,
    pub issued_at_utc_ms: i64,
    pub expires_at_utc_ms: i64,
    pub scope_sha256: String,
    pub attestation_sha256: String,
}

/// The trust boundary for catalog and as-run evidence. Implementations must
/// authenticate `receipt.attestation_sha256` and bind it to every supplied
/// argument; an accept-all implementation is not a production verifier.
pub trait SourceEvidenceVerifier {
    fn verify(
        &self,
        kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        payload_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedCandidateProjection {
    pub receipt: SourceEvidenceReceipt,
    pub candidates: Vec<AssetCandidate>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedPriorAirings {
    pub receipt: SourceEvidenceReceipt,
    pub airings: Vec<PriorAiring>,
}

/// Raw slices are intentionally not accepted by materialization. Even an
/// empty projection is evidence only after both its canonical digest and its
/// receipt have been verified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedMaterializationInputs {
    pub candidate_projection: AuthenticatedCandidateProjection,
    pub prior_airings: AuthenticatedPriorAirings,
}

#[derive(Debug, Clone, Copy)]
pub struct MaterializationAuthorities<'a> {
    pub search: &'a SavedSearch,
    pub block: &'a DaypartBlock,
    pub rule: &'a RecurringRule,
}

/// Exact, immutable source evidence carried in a plan and draft intent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationInputBinding {
    pub candidate_projection_sha256: String,
    pub candidate_receipt_sha256: String,
    pub prior_airings_sha256: String,
    pub prior_airings_receipt_sha256: String,
    pub scope_sha256: String,
    pub combined_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssetResolution {
    Selected(AssetCandidate),
    NoMatch,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DaypartBlock {
    pub block_id: String,
    pub channel_id: String,
    pub label: String,
    pub starts_local_minute: u16,
    pub ends_local_minute: u16,
    pub weekday_mask: u8,
    pub season_starts_on: Option<LocalDate>,
    pub season_ends_on: Option<LocalDate>,
    pub allow_rule_ids: Vec<String>,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum DstApplication {
    Standard,
    EarlierFold,
    LaterFold,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum LocalResolution {
    Normal,
    EarlierFold,
    LaterFold,
    Gap,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AssetPickStrategy {
    Top,
    Random,
    Newest,
    LeastRecentlyAired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum RecurrenceTimeZone {
    Utc,
    Named {
        iana_name: String,
        dst: DstApplication,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct LocalDate {
    pub year: u16,
    pub month: u8,
    pub day: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecurringRule {
    pub rule_id: String,
    pub channel_id: String,
    pub saved_search_id: String,
    pub saved_search_revision: u64,
    pub pick_strategy: AssetPickStrategy,
    pub rolling_window_days: u8,
    pub avoid_repeat_within_days: Option<u8>,
    pub enabled: bool,
    pub priority: i16,
    pub block_id: String,
    pub block_revision: u64,
    pub weekday_mask: u8,
    pub interval_weeks: u8,
    pub anchor_date: LocalDate,
    pub timezone: RecurrenceTimeZone,
    pub local_start_minute: u16,
    pub duration_minutes: u16,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MaterializationWindow {
    pub starts_at_utc_ms: i64,
    pub ends_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSlot {
    occurrence_id: String,
    asset_sha256: String,
    asset_title: String,
    local_date: LocalDate,
    local_minute: u16,
    iana_zone: String,
    utc_offset_seconds: i32,
    starts_at_utc_ms: i64,
    ends_at_utc_ms: i64,
    resolution: LocalResolution,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationIntent {
    pub hash_schema_version: u16,
    pub materialization_id: String,
    pub rule_id: String,
    pub rule_revision: u64,
    pub channel_id: String,
    pub block_id: String,
    pub block_revision: u64,
    pub window: MaterializationWindow,
    pub slots: Vec<ResolvedSlot>,
    pub source_authority_sha256: String,
    pub source_input_binding: MaterializationInputBinding,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializedOccurrence {
    pub occurrence_id: String,
    pub asset_sha256: String,
    pub asset_title: String,
    pub starts_at_utc_ms: i64,
    pub ends_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MaterializationAdmission {
    Create(Box<MaterializationIntent>),
    AlreadyPresent,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MaterializationOutcome {
    Plan(MaterializationPlan),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationPlan {
    pub source_authority_sha256: String,
    pub source_input_binding: MaterializationInputBinding,
    /// Draft-only: this pure layer never commits the derived schedule items.
    pub draft_intent: Option<Box<MaterializationIntent>>,
    pub outcomes: Vec<SlotMaterializationOutcome>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SlotMaterializationOutcome {
    Proposed(MaterializedOccurrence),
    NoMatch {
        starts_at_utc_ms: i64,
    },
    Blocked {
        starts_at_utc_ms: i64,
    },
    SuppressedByHigherPriority {
        rule_id: String,
        starts_at_utc_ms: i64,
        winner_rule_id: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuleSlotCandidate {
    pub rule_id: String,
    pub priority: i16,
    pub starts_at_utc_ms: i64,
    pub ends_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ArbitrationOutcome {
    Winner {
        rule_id: String,
        starts_at_utc_ms: i64,
    },
    SuppressedByHigherPriority {
        rule_id: String,
        starts_at_utc_ms: i64,
        winner_rule_id: String,
    },
}

impl SavedSearch {
    pub fn validate(&self) -> Result<(), String> {
        ids(&[&self.search_id, &self.channel_id, &self.owner_operator_id])?;
        if self.revision == 0 || self.query.validate().is_err() {
            return Err("saved search authority is unbounded, unordered, or malformed".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(&self, current: &Self, expected: u64) -> Result<(), String> {
        self.validate()?;
        current.validate()?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("search revision overflow")?
            || self.search_id != current.search_id
            || self.channel_id != current.channel_id
            || self.owner_operator_id != current.owner_operator_id
        {
            return Err("saved search CAS or identity rebinding failed".into());
        }
        Ok(())
    }
}

impl SavedSearchQuery {
    pub fn validate(&self) -> Result<(), String> {
        if self.all_of.len() > MAX_QUERY_FILTERS
            || self.any_of.len() > MAX_QUERY_FILTERS
            || self.all_of.is_empty() && self.any_of.is_empty()
            || self.limit == 0
            || usize::from(self.limit) > MAX_QUERY_LIMIT
            || self.all_of.iter().chain(&self.any_of).any(|filter| {
                !safe_metadata_field(&filter.field) || !valid_metadata_value(&filter.value)
            })
        {
            return Err("saved-search query is unbounded or malformed".into());
        }
        Ok(())
    }
}

impl AssetCandidate {
    pub fn validate(&self) -> Result<(), String> {
        if !lowercase_sha256(&self.asset_sha256)
            || !safe_schedule_title(&self.title)
            || self.duration_ms == 0
            || self.published_at_utc_ms < 0
            || self.indexed_custom_metadata.len() > MAX_QUERY_LIMIT
            || self
                .indexed_custom_metadata
                .iter()
                .any(|(key, value)| !safe_metadata_field(key) || !valid_metadata_value(value))
        {
            return Err("asset candidate projection is malformed or unbounded".into());
        }
        Ok(())
    }
}

pub fn resolve_asset(
    search: &SavedSearch,
    rule: &RecurringRule,
    candidates: &[AssetCandidate],
    occurrence_starts_at_utc_ms: i64,
    prior_airings: &[PriorAiring],
) -> Result<AssetResolution, String> {
    search.validate()?;
    rule.validate()?;
    if candidates.len() > MAX_QUERY_LIMIT || prior_airings.len() > MAX_QUERY_LIMIT {
        return Err("asset resolution input is unbounded".into());
    }
    for candidate in candidates {
        candidate.validate()?;
    }
    for airing in prior_airings {
        if !lowercase_sha256(&airing.asset_sha256) || airing.aired_at_utc_ms < 0 {
            return Err("prior airing projection is malformed".into());
        }
    }
    let repeat_cutoff = rule
        .avoid_repeat_within_days
        .map(|days| occurrence_starts_at_utc_ms - i64::from(days) * 86_400_000);
    let mut matches = candidates
        .iter()
        .filter(|candidate| candidate.ready && candidate.published)
        .filter(|candidate| candidate.published_at_utc_ms <= occurrence_starts_at_utc_ms)
        .filter(|candidate| query_matches(&search.query, candidate))
        .filter(|candidate| {
            repeat_cutoff.is_none_or(|cutoff| {
                !prior_airings.iter().any(|airing| {
                    airing.asset_sha256 == candidate.asset_sha256
                        && airing.aired_at_utc_ms >= cutoff
                        && airing.aired_at_utc_ms <= occurrence_starts_at_utc_ms
                })
            })
        })
        .cloned()
        .collect::<Vec<_>>();
    order_candidates(&mut matches, search.query.order_by);
    matches.truncate(usize::from(search.query.limit));
    if matches.is_empty() {
        return Ok(AssetResolution::NoMatch);
    }
    let selected = match rule.pick_strategy {
        AssetPickStrategy::Top => matches.remove(0),
        AssetPickStrategy::Newest => {
            matches.sort_by(|left, right| {
                right
                    .published_at_utc_ms
                    .cmp(&left.published_at_utc_ms)
                    .then_with(|| left.asset_sha256.cmp(&right.asset_sha256))
            });
            matches.remove(0)
        }
        AssetPickStrategy::LeastRecentlyAired => {
            matches.sort_by(|left, right| {
                last_aired(prior_airings, &left.asset_sha256)
                    .cmp(&last_aired(prior_airings, &right.asset_sha256))
                    .then_with(|| left.asset_sha256.cmp(&right.asset_sha256))
            });
            matches.remove(0)
        }
        AssetPickStrategy::Random => matches.remove(
            (selection_seed(&rule.rule_id, &rule.channel_id, occurrence_starts_at_utc_ms) as usize)
                % matches.len(),
        ),
    };
    Ok(AssetResolution::Selected(selected))
}

fn query_matches(query: &SavedSearchQuery, candidate: &AssetCandidate) -> bool {
    let matches = |filter: &MetadataFilter| {
        candidate
            .indexed_custom_metadata
            .get(&filter.field)
            .is_some_and(|value| value == &filter.value)
    };
    query.all_of.iter().all(matches)
        && (query.any_of.is_empty() || query.any_of.iter().any(matches))
}

fn last_aired(prior_airings: &[PriorAiring], asset_sha256: &str) -> Option<i64> {
    prior_airings
        .iter()
        .filter(|airing| airing.asset_sha256 == asset_sha256)
        .map(|airing| airing.aired_at_utc_ms)
        .max()
}

fn valid_metadata_value(value: &MetadataValue) -> bool {
    match value {
        MetadataValue::Text(value) | MetadataValue::Choice(value) => safe_schedule_title(value),
        MetadataValue::Number(_) | MetadataValue::Boolean(_) => true,
        MetadataValue::DateTimeUnixMs(value) => *value >= 0,
    }
}

fn order_candidates(candidates: &mut [AssetCandidate], order: SavedSearchOrder) {
    candidates.sort_by(|left, right| match order {
        SavedSearchOrder::TitleAscending => left
            .title
            .cmp(&right.title)
            .then_with(|| left.asset_sha256.cmp(&right.asset_sha256)),
        SavedSearchOrder::PublishedNewest => right
            .published_at_utc_ms
            .cmp(&left.published_at_utc_ms)
            .then_with(|| left.asset_sha256.cmp(&right.asset_sha256)),
    });
}

impl DaypartBlock {
    pub fn validate(&self) -> Result<(), String> {
        ids(&[&self.block_id, &self.channel_id])?;
        if self.revision == 0
            || !safe_label(&self.label)
            || self.starts_local_minute >= self.ends_local_minute
            || self.ends_local_minute > 1_440
            || self.weekday_mask == 0
            || self.weekday_mask & !0b0111_1111 != 0
            || self.season_starts_on.is_some() != self.season_ends_on.is_some()
            || self
                .season_starts_on
                .as_ref()
                .zip(self.season_ends_on.as_ref())
                .is_some_and(|(start, end)| start > end)
            || self
                .season_starts_on
                .as_ref()
                .is_some_and(|date| date.validate().is_err())
            || self
                .season_ends_on
                .as_ref()
                .is_some_and(|date| date.validate().is_err())
            || self.allow_rule_ids.len() > MAX_QUERY_LIMIT
            || self.allow_rule_ids.iter().any(|rule_id| !uuid(rule_id))
            || self
                .allow_rule_ids
                .windows(2)
                .any(|pair| pair[0] >= pair[1])
        {
            return Err("daypart authority is malformed or unbounded".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(&self, current: &Self, expected: u64) -> Result<(), String> {
        self.validate()?;
        current.validate()?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("block revision overflow")?
            || self.block_id != current.block_id
            || self.channel_id != current.channel_id
        {
            return Err("daypart CAS or identity rebinding failed".into());
        }
        Ok(())
    }
}

impl RecurringRule {
    pub fn validate_against(
        &self,
        search: &SavedSearch,
        block: &DaypartBlock,
    ) -> Result<(), String> {
        self.validate()?;
        search.validate()?;
        block.validate()?;
        if self.channel_id != search.channel_id
            || self.channel_id != block.channel_id
            || self.saved_search_id != search.search_id
            || self.saved_search_revision != search.revision
            || self.block_id != block.block_id
            || self.block_revision != block.revision
            || (!block.allow_rule_ids.is_empty()
                && block.allow_rule_ids.binary_search(&self.rule_id).is_err())
            || self.weekday_mask & block.weekday_mask == 0
            || self.local_start_minute < block.starts_local_minute
            || self
                .local_start_minute
                .checked_add(self.duration_minutes)
                .ok_or("rule duration overflow")?
                > block.ends_local_minute
        {
            return Err(
                "recurring rule search, channel, or block authority is stale or rebound".into(),
            );
        }
        Ok(())
    }
    pub fn validate(&self) -> Result<(), String> {
        ids(&[
            &self.rule_id,
            &self.channel_id,
            &self.saved_search_id,
            &self.block_id,
        ])?;
        self.anchor_date.validate()?;
        validate_timezone(&self.timezone)?;
        if self.revision == 0
            || self.saved_search_revision == 0
            || self.block_revision == 0
            || !(14..=60).contains(&self.rolling_window_days)
            || self
                .avoid_repeat_within_days
                .is_some_and(|days| !(1..=60).contains(&days))
            || self.weekday_mask == 0
            || self.weekday_mask & !0b0111_1111 != 0
            || !(1..=4).contains(&self.interval_weeks)
            || self.local_start_minute >= 1_440
            || self.duration_minutes == 0
            || self.duration_minutes > 360
        {
            return Err("recurring rule recurrence is unbounded or malformed".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(
        &self,
        current: &Self,
        expected: u64,
        current_search: &SavedSearch,
        current_block: &DaypartBlock,
        next_search: &SavedSearch,
        next_block: &DaypartBlock,
    ) -> Result<(), String> {
        current.validate_against(current_search, current_block)?;
        self.validate_against(next_search, next_block)?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("rule revision overflow")?
            || self.rule_id != current.rule_id
            || self.channel_id != current.channel_id
            || self.saved_search_id != current.saved_search_id
            || self.block_id != current.block_id
        {
            return Err("recurring rule CAS or channel/asset rebinding failed".into());
        }
        Ok(())
    }
}

impl AuthenticatedMaterializationInputs {
    fn verify_and_bind(
        &self,
        source_authority_sha256: &str,
        window: &MaterializationWindow,
        observed_at_utc_ms: i64,
        verifier: &impl SourceEvidenceVerifier,
    ) -> Result<MaterializationInputBinding, String> {
        if self.candidate_projection.receipt.issuer_id == self.prior_airings.receipt.issuer_id
            && self.candidate_projection.receipt.receipt_id == self.prior_airings.receipt.receipt_id
        {
            return Err("candidate and prior-airings evidence must have distinct receipts".into());
        }
        let candidate_projection_sha256 = projection_hash(
            SourceEvidenceKind::CandidateProjection,
            &canonical_candidate_projection_bytes(&self.candidate_projection.candidates)?,
        );
        let prior_airings_sha256 = projection_hash(
            SourceEvidenceKind::PriorAirings,
            &canonical_prior_airings_bytes(&self.prior_airings.airings)?,
        );
        let candidate_scope = evidence_scope_hash(
            SourceEvidenceKind::CandidateProjection,
            source_authority_sha256,
            window,
        );
        let airing_scope = evidence_scope_hash(
            SourceEvidenceKind::PriorAirings,
            source_authority_sha256,
            window,
        );
        self.candidate_projection
            .receipt
            .validate_for(&candidate_scope, observed_at_utc_ms)?;
        self.prior_airings
            .receipt
            .validate_for(&airing_scope, observed_at_utc_ms)?;
        verifier.verify(
            SourceEvidenceKind::CandidateProjection,
            &self.candidate_projection.receipt,
            &candidate_scope,
            &candidate_projection_sha256,
            observed_at_utc_ms,
        )?;
        verifier.verify(
            SourceEvidenceKind::PriorAirings,
            &self.prior_airings.receipt,
            &airing_scope,
            &prior_airings_sha256,
            observed_at_utc_ms,
        )?;
        let binding = MaterializationInputBinding {
            candidate_projection_sha256,
            candidate_receipt_sha256: receipt_hash(&self.candidate_projection.receipt),
            prior_airings_sha256,
            prior_airings_receipt_sha256: receipt_hash(&self.prior_airings.receipt),
            scope_sha256: combined_scope_hash(&candidate_scope, &airing_scope),
            combined_sha256: String::new(),
        };
        let mut binding = binding;
        binding.combined_sha256 = input_binding_hash(&binding);
        binding.validate_for(source_authority_sha256, window)?;
        Ok(binding)
    }
}

impl SourceEvidenceReceipt {
    fn validate_for(
        &self,
        expected_scope_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        ids(&[&self.issuer_id, &self.receipt_id])?;
        if observed_at_utc_ms < 0
            || self.issued_at_utc_ms < 0
            || self.issued_at_utc_ms > observed_at_utc_ms
            || self.expires_at_utc_ms <= observed_at_utc_ms
            || self.expires_at_utc_ms <= self.issued_at_utc_ms
            || self.expires_at_utc_ms - self.issued_at_utc_ms > MAX_EVIDENCE_RECEIPT_LIFETIME_MS
            || !lowercase_sha256(&self.scope_sha256)
            || !lowercase_sha256(&self.attestation_sha256)
            || self.scope_sha256 != expected_scope_sha256
        {
            return Err("source evidence receipt is malformed, expired, or scope-rebound".into());
        }
        Ok(())
    }
}

impl MaterializationInputBinding {
    fn validate_for(
        &self,
        source_authority_sha256: &str,
        window: &MaterializationWindow,
    ) -> Result<(), String> {
        if !lowercase_sha256(source_authority_sha256)
            || [
                &self.candidate_projection_sha256,
                &self.candidate_receipt_sha256,
                &self.prior_airings_sha256,
                &self.prior_airings_receipt_sha256,
                &self.scope_sha256,
                &self.combined_sha256,
            ]
            .iter()
            .any(|value| !lowercase_sha256(value))
            || self.scope_sha256
                != combined_scope_hash(
                    &evidence_scope_hash(
                        SourceEvidenceKind::CandidateProjection,
                        source_authority_sha256,
                        window,
                    ),
                    &evidence_scope_hash(
                        SourceEvidenceKind::PriorAirings,
                        source_authority_sha256,
                        window,
                    ),
                )
            || self.combined_sha256 != input_binding_hash(self)
        {
            return Err("materialization input evidence is forged, incomplete, or rebound".into());
        }
        Ok(())
    }
}

pub fn derive_materialization_intent(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
    window: MaterializationWindow,
    observed_at_utc_ms: i64,
    inputs: &AuthenticatedMaterializationInputs,
    verifier: &impl SourceEvidenceVerifier,
) -> Result<MaterializationOutcome, String> {
    rule.validate_against(search, block)?;
    window.validate()?;
    if window.ends_at_utc_ms - window.starts_at_utc_ms
        > i64::from(rule.rolling_window_days) * 86_400_000
    {
        return Err("materialization window exceeds the rule rolling horizon".into());
    }
    let source_authority_sha256 = authority_hash(search, block, rule);
    let source_input_binding = inputs.verify_and_bind(
        &source_authority_sha256,
        &window,
        observed_at_utc_ms,
        verifier,
    )?;
    let mut slots = resolve_slots(rule, &window)?;
    let mut selected_airings = inputs.prior_airings.airings.clone();
    let mut outcomes = Vec::with_capacity(slots.len());
    for slot in &mut slots {
        if !rule.enabled || !block_applies_on(block, &slot.local_date) {
            outcomes.push(SlotMaterializationOutcome::Blocked {
                starts_at_utc_ms: slot.starts_at_utc_ms,
            });
            continue;
        }
        let resolution = resolve_asset(
            search,
            rule,
            &inputs.candidate_projection.candidates,
            slot.starts_at_utc_ms,
            &selected_airings,
        )?;
        let AssetResolution::Selected(asset) = resolution else {
            outcomes.push(SlotMaterializationOutcome::NoMatch {
                starts_at_utc_ms: slot.starts_at_utc_ms,
            });
            continue;
        };
        slot.asset_sha256 = asset.asset_sha256.clone();
        slot.asset_title = asset.title.clone();
        slot.occurrence_id =
            occurrence_hash(&rule.rule_id, &rule.channel_id, slot.starts_at_utc_ms);
        selected_airings.push(PriorAiring {
            asset_sha256: asset.asset_sha256,
            aired_at_utc_ms: slot.starts_at_utc_ms,
        });
        outcomes.push(SlotMaterializationOutcome::Proposed(
            MaterializedOccurrence {
                occurrence_id: slot.occurrence_id.clone(),
                asset_sha256: slot.asset_sha256.clone(),
                asset_title: slot.asset_title.clone(),
                starts_at_utc_ms: slot.starts_at_utc_ms,
                ends_at_utc_ms: slot.ends_at_utc_ms,
            },
        ));
    }
    let proposed_slots = slots
        .into_iter()
        .filter(|slot| !slot.occurrence_id.is_empty())
        .collect::<Vec<_>>();
    let draft_intent = if proposed_slots.is_empty() {
        None
    } else {
        let mut intent = MaterializationIntent {
            hash_schema_version: HASH_SCHEMA_VERSION,
            materialization_id: String::new(),
            rule_id: rule.rule_id.clone(),
            rule_revision: rule.revision,
            channel_id: rule.channel_id.clone(),
            block_id: block.block_id.clone(),
            block_revision: block.revision,
            window,
            slots: proposed_slots,
            source_authority_sha256: source_authority_sha256.clone(),
            source_input_binding: source_input_binding.clone(),
        };
        intent.materialization_id = intent_hash(&intent);
        intent.validate_integrity_against(search, block, rule)?;
        Some(Box::new(intent))
    };
    Ok(MaterializationOutcome::Plan(MaterializationPlan {
        source_authority_sha256,
        source_input_binding,
        draft_intent,
        outcomes,
    }))
}

fn block_applies_on(block: &DaypartBlock, date: &LocalDate) -> bool {
    block.weekday_mask & (1 << date.weekday()) != 0
        && block
            .season_starts_on
            .as_ref()
            .zip(block.season_ends_on.as_ref())
            .is_none_or(|(start, end)| date >= start && date <= end)
}

pub fn arbitrate_rule_slots(
    candidates: &[RuleSlotCandidate],
) -> Result<Vec<ArbitrationOutcome>, String> {
    if candidates.len() > MAX_SLOTS {
        return Err("rule overlap arbitration input is unbounded".into());
    }
    for candidate in candidates {
        if !uuid(&candidate.rule_id)
            || candidate.starts_at_utc_ms < 0
            || candidate.ends_at_utc_ms <= candidate.starts_at_utc_ms
        {
            return Err("rule overlap arbitration candidate is malformed".into());
        }
    }
    let mut ranked = candidates.to_vec();
    ranked.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| left.rule_id.cmp(&right.rule_id))
            .then_with(|| left.starts_at_utc_ms.cmp(&right.starts_at_utc_ms))
    });
    let mut winners: Vec<RuleSlotCandidate> = Vec::new();
    let mut outcomes = Vec::with_capacity(ranked.len());
    for candidate in ranked {
        if let Some(winner) = winners.iter().find(|winner| {
            candidate.starts_at_utc_ms < winner.ends_at_utc_ms
                && winner.starts_at_utc_ms < candidate.ends_at_utc_ms
        }) {
            outcomes.push(ArbitrationOutcome::SuppressedByHigherPriority {
                rule_id: candidate.rule_id.clone(),
                starts_at_utc_ms: candidate.starts_at_utc_ms,
                winner_rule_id: winner.rule_id.clone(),
            });
        } else {
            outcomes.push(ArbitrationOutcome::Winner {
                rule_id: candidate.rule_id.clone(),
                starts_at_utc_ms: candidate.starts_at_utc_ms,
            });
            winners.push(candidate);
        }
    }
    Ok(outcomes)
}

pub fn admit_materialization(
    existing: &[MaterializationIntent],
    mut candidate: MaterializationIntent,
    authorities: MaterializationAuthorities<'_>,
    inputs: &AuthenticatedMaterializationInputs,
    observed_at_utc_ms: i64,
    verifier: &impl SourceEvidenceVerifier,
) -> Result<MaterializationAdmission, String> {
    let MaterializationAuthorities {
        search,
        block,
        rule,
    } = authorities;
    if existing.len() > MAX_SLOTS {
        return Err("materialization ledger is unbounded".into());
    }
    for (index, prior) in existing.iter().enumerate() {
        // Historical intents are immutable audit records. Current rule/search/block
        // authority may legitimately have advanced after they were appended.
        prior.validate()?;
        if existing[..index]
            .iter()
            .any(|older| older.materialization_id == prior.materialization_id)
        {
            return Err("duplicate materialization identity is not appendable".into());
        }
    }
    candidate.validate_authenticated_against(
        search,
        block,
        rule,
        inputs,
        observed_at_utc_ms,
        verifier,
    )?;
    candidate.slots.retain(|slot| {
        !existing.iter().any(|prior| {
            prior
                .slots
                .iter()
                .any(|prior_slot| prior_slot.occurrence_id == slot.occurrence_id)
        })
    });
    if candidate.slots.is_empty() {
        return Ok(MaterializationAdmission::AlreadyPresent);
    }
    candidate.materialization_id = intent_hash(&candidate);
    candidate.validate_authenticated_against(
        search,
        block,
        rule,
        inputs,
        observed_at_utc_ms,
        verifier,
    )?;
    if let Some(prior) = existing
        .iter()
        .find(|prior| prior.materialization_id == candidate.materialization_id)
    {
        return if prior == &candidate {
            Ok(MaterializationAdmission::AlreadyPresent)
        } else {
            Err("materialization identity collision is forged".into())
        };
    }
    if existing.len() == MAX_SLOTS {
        return Err("materialization ledger is at append capacity".into());
    }
    Ok(MaterializationAdmission::Create(Box::new(candidate)))
}

impl MaterializationWindow {
    fn validate(&self) -> Result<(), String> {
        if self.starts_at_utc_ms < 0
            || self.ends_at_utc_ms <= self.starts_at_utc_ms
            || self.ends_at_utc_ms - self.starts_at_utc_ms > MAX_WINDOW_MS
        {
            Err("materialization UTC window is ambiguous or unbounded".into())
        } else {
            Ok(())
        }
    }
}

impl MaterializationIntent {
    pub fn resolved_slots(&self) -> Vec<(i64, i32, LocalResolution)> {
        self.slots
            .iter()
            .map(|slot| {
                (
                    slot.starts_at_utc_ms,
                    slot.utc_offset_seconds,
                    slot.resolution,
                )
            })
            .collect()
    }
    pub fn occurrence_ids(&self) -> Vec<String> {
        self.slots
            .iter()
            .map(|slot| slot.occurrence_id.clone())
            .collect()
    }
    pub fn materialized_occurrences(&self) -> Vec<MaterializedOccurrence> {
        self.slots
            .iter()
            .map(|slot| MaterializedOccurrence {
                occurrence_id: slot.occurrence_id.clone(),
                asset_sha256: slot.asset_sha256.clone(),
                asset_title: slot.asset_title.clone(),
                starts_at_utc_ms: slot.starts_at_utc_ms,
                ends_at_utc_ms: slot.ends_at_utc_ms,
            })
            .collect()
    }
    fn validate_integrity_against(
        &self,
        search: &SavedSearch,
        block: &DaypartBlock,
        rule: &RecurringRule,
    ) -> Result<(), String> {
        self.validate()?;
        rule.validate_against(search, block)?;
        if self.rule_id != rule.rule_id
            || self.rule_revision != rule.revision
            || self.channel_id != rule.channel_id
            || self.block_id != block.block_id
            || self.block_revision != block.revision
            || self.source_authority_sha256 != authority_hash(search, block, rule)
        {
            return Err("materialization source authority is stale or rebound".into());
        }
        self.source_input_binding
            .validate_for(&self.source_authority_sha256, &self.window)?;
        validate_slots(rule, &self.window, &self.slots)
    }
    pub fn validate_authenticated_against(
        &self,
        search: &SavedSearch,
        block: &DaypartBlock,
        rule: &RecurringRule,
        inputs: &AuthenticatedMaterializationInputs,
        observed_at_utc_ms: i64,
        verifier: &impl SourceEvidenceVerifier,
    ) -> Result<(), String> {
        self.validate_integrity_against(search, block, rule)?;
        let expected = inputs.verify_and_bind(
            &self.source_authority_sha256,
            &self.window,
            observed_at_utc_ms,
            verifier,
        )?;
        if self.source_input_binding != expected {
            return Err("materialization input evidence was not authenticated".into());
        }
        Ok(())
    }
    fn validate(&self) -> Result<(), String> {
        ids(&[&self.rule_id, &self.channel_id, &self.block_id])?;
        self.window.validate()?;
        if self.rule_revision == 0
            || self.block_revision == 0
            || self.slots.is_empty()
            || self.hash_schema_version != HASH_SCHEMA_VERSION
            || self.slots.len() > MAX_SLOTS
            || !sha(&self.source_authority_sha256)
            || self
                .source_input_binding
                .validate_for(&self.source_authority_sha256, &self.window)
                .is_err()
            || self.materialization_id != intent_hash(self)
        {
            return Err("materialization authority is forged or unbounded".into());
        }
        for (index, slot) in self.slots.iter().enumerate() {
            slot.local_date.validate()?;
            if slot.starts_at_utc_ms < self.window.starts_at_utc_ms
                || slot.ends_at_utc_ms > self.window.ends_at_utc_ms
                || slot.ends_at_utc_ms <= slot.starts_at_utc_ms
                || (index > 0 && self.slots[index - 1].starts_at_utc_ms >= slot.starts_at_utc_ms)
            {
                return Err("materialization slot shape is invalid or unordered".into());
            }
            if slot.occurrence_id
                != occurrence_hash(&self.rule_id, &self.channel_id, slot.starts_at_utc_ms)
            {
                return Err("materialization occurrence identity is forged".into());
            }
            if !lowercase_sha256(&slot.asset_sha256) || !safe_schedule_title(&slot.asset_title) {
                return Err("materialization asset identity is not a lowercase SHA-256".into());
            }
        }
        Ok(())
    }
}

fn resolve_slots(
    rule: &RecurringRule,
    window: &MaterializationWindow,
) -> Result<Vec<ResolvedSlot>, String> {
    let (zone_name, zone) = match &rule.timezone {
        RecurrenceTimeZone::Utc => ("UTC", TimeZone::UTC),
        RecurrenceTimeZone::Named { iana_name, .. } => (
            iana_name.as_str(),
            TimeZone::get(iana_name).map_err(|error| format!("timezone lookup failed: {error}"))?,
        ),
    };
    let mut date = rule.anchor_date.clone();
    let mut slots = Vec::new();
    while date.year <= 2100 {
        if rule.weekday_mask & (1 << date.weekday()) != 0
            && ((date.days_since_epoch() - rule.anchor_date.days_since_epoch()) / 7)
                % i64::from(rule.interval_weeks)
                == 0
        {
            let civil: DateTime = format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:00",
                date.year,
                date.month,
                date.day,
                rule.local_start_minute / 60,
                rule.local_start_minute % 60
            )
            .parse()
            .map_err(|error| format!("local recurrence datetime failed: {error}"))?;
            let normal = zone.to_ambiguous_zoned(civil).unambiguous();
            let (zoned, resolution) = if let Ok(value) = normal {
                (value, LocalResolution::Normal)
            } else {
                let earlier = zone
                    .to_ambiguous_zoned(civil)
                    .earlier()
                    .map_err(|error| format!("earlier fold resolution failed: {error}"))?;
                let later = zone
                    .to_ambiguous_zoned(civil)
                    .later()
                    .map_err(|error| format!("later fold resolution failed: {error}"))?;
                if earlier.datetime() != civil || later.datetime() != civil {
                    let near_window = [
                        earlier.timestamp().as_millisecond(),
                        later.timestamp().as_millisecond(),
                    ]
                    .iter()
                    .any(|instant| {
                        *instant >= window.starts_at_utc_ms && *instant < window.ends_at_utc_ms
                    });
                    if near_window {
                        return Err(
                            "nonexistent local recurrence time falls in materialization window"
                                .into(),
                        );
                    }
                    date = date.next_day()?;
                    continue;
                }
                match &rule.timezone {
                    RecurrenceTimeZone::Named {
                        dst: DstApplication::EarlierFold,
                        ..
                    } => (earlier, LocalResolution::EarlierFold),
                    RecurrenceTimeZone::Named {
                        dst: DstApplication::LaterFold,
                        ..
                    } => (later, LocalResolution::LaterFold),
                    _ => return Err("ambiguous local recurrence lacks explicit fold policy".into()),
                }
            };
            let starts_at_utc_ms = zoned.timestamp().as_millisecond();
            if starts_at_utc_ms >= window.ends_at_utc_ms
                && date.days_since_epoch() > rule.anchor_date.days_since_epoch()
            {
                break;
            }
            if starts_at_utc_ms >= window.starts_at_utc_ms
                && starts_at_utc_ms < window.ends_at_utc_ms
            {
                slots.push(ResolvedSlot {
                    occurrence_id: String::new(),
                    asset_sha256: String::new(),
                    asset_title: String::new(),
                    local_date: date.clone(),
                    local_minute: rule.local_start_minute,
                    iana_zone: zone_name.to_owned(),
                    utc_offset_seconds: zoned.offset().seconds(),
                    starts_at_utc_ms,
                    ends_at_utc_ms: starts_at_utc_ms + i64::from(rule.duration_minutes) * 60_000,
                    resolution,
                });
                if slots.len() > MAX_SLOTS {
                    return Err("resolved recurrence exceeds bounded slot count".into());
                }
            }
        }
        date = date.next_day()?;
    }
    Ok(slots)
}

fn validate_slots(
    rule: &RecurringRule,
    window: &MaterializationWindow,
    slots: &[ResolvedSlot],
) -> Result<(), String> {
    if slots.is_empty() || slots.len() > MAX_SLOTS {
        return Err("materialization slots are unbounded".into());
    }
    for (index, slot) in slots.iter().enumerate() {
        slot.local_date.validate()?;
        let zone_ok = match &rule.timezone {
            RecurrenceTimeZone::Utc => {
                slot.iana_zone == "UTC"
                    && slot.utc_offset_seconds == 0
                    && slot.resolution == LocalResolution::Normal
            }
            RecurrenceTimeZone::Named { iana_name, dst } => {
                slot.iana_zone == *iana_name
                    && matches!(
                        (dst, slot.resolution),
                        (_, LocalResolution::Normal)
                            | (DstApplication::EarlierFold, LocalResolution::EarlierFold)
                            | (DstApplication::LaterFold, LocalResolution::LaterFold)
                    )
            }
        };
        let local_ms =
            slot.local_date.days_since_epoch() * 86_400_000 + i64::from(slot.local_minute) * 60_000;
        if slot.starts_at_utc_ms < window.starts_at_utc_ms
            || slot.ends_at_utc_ms > window.ends_at_utc_ms
            || slot.ends_at_utc_ms - slot.starts_at_utc_ms
                != i64::from(rule.duration_minutes) * 60_000
            || slot.local_date.days_since_epoch() < rule.anchor_date.days_since_epoch()
            || slot.local_minute != rule.local_start_minute
            || !(-50_400..=50_400).contains(&slot.utc_offset_seconds)
            || slot.starts_at_utc_ms != local_ms - i64::from(slot.utc_offset_seconds) * 1_000
            || !zone_ok
            || slot.resolution == LocalResolution::Gap
            || rule.weekday_mask & (1 << slot.local_date.weekday()) == 0
            || ((slot.local_date.days_since_epoch() - rule.anchor_date.days_since_epoch()) / 7)
                % i64::from(rule.interval_weeks)
                != 0
            || (index > 0
                && (slots[index - 1].starts_at_utc_ms >= slot.starts_at_utc_ms
                    || slots[index - 1].local_date >= slot.local_date))
        {
            return Err("materialization slot does not deterministically match bounded recurrence authority".into());
        }
    }
    Ok(())
}

impl LocalDate {
    fn validate(&self) -> Result<(), String> {
        if !(1970..=2100).contains(&self.year)
            || self.month == 0
            || self.month > 12
            || self.day == 0
            || self.day > month_days(self.year, self.month)
        {
            Err("local recurrence date is invalid".into())
        } else {
            Ok(())
        }
    }
    fn days_since_epoch(&self) -> i64 {
        let years = (1970..self.year)
            .map(|year| if leap(year) { 366 } else { 365 })
            .sum::<i64>();
        years
            + (1..self.month)
                .map(|month| i64::from(month_days(self.year, month)))
                .sum::<i64>()
            + i64::from(self.day - 1)
    }
    fn weekday(&self) -> u8 {
        ((3 + self.days_since_epoch()) % 7) as u8
    }
    fn next_day(&self) -> Result<Self, String> {
        self.validate()?;
        let mut next = self.clone();
        if next.day < month_days(next.year, next.month) {
            next.day += 1;
        } else if next.month < 12 {
            next.month += 1;
            next.day = 1;
        } else {
            next.year = next.year.checked_add(1).ok_or("local date overflow")?;
            next.month = 1;
            next.day = 1;
        }
        Ok(next)
    }
}

fn validate_timezone(value: &RecurrenceTimeZone) -> Result<(), String> {
    match value {
        RecurrenceTimeZone::Utc => Ok(()),
        RecurrenceTimeZone::Named {
            iana_name,
            dst: DstApplication::EarlierFold | DstApplication::LaterFold,
        } if matches!(
            iana_name.as_str(),
            "America/Denver" | "America/Chicago" | "America/New_York" | "America/Los_Angeles"
        ) =>
        {
            Ok(())
        }
        _ => Err("recurrence timezone or DST fold policy is ambiguous".into()),
    }
}
fn month_days(year: u16, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap(year) => 29,
        _ => 28,
    }
}
fn leap(year: u16) -> bool {
    year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400))
}
fn safe_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_LABEL_LEN
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b' ' | b'-'))
}
fn safe_schedule_title(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 240
        && value == value.trim()
        && value.chars().all(|character| !character.is_control())
}
fn safe_metadata_field(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_TERM_LEN
        && value
            .bytes()
            .next()
            .is_some_and(|byte| byte.is_ascii_lowercase())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}
fn ids(values: &[&str]) -> Result<(), String> {
    for value in values {
        if !uuid(value) {
            return Err("schedule automation identity must be a UUID".into());
        }
    }
    Ok(())
}
fn uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            [8, 13, 18, 23].contains(&index) || byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')
        })
}
fn sha(value: &str) -> bool {
    lowercase_sha256(value)
}
fn lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}
trait CanonicalSink {
    fn write(&mut self, bytes: impl AsRef<[u8]>);
}
impl CanonicalSink for Sha256 {
    fn write(&mut self, bytes: impl AsRef<[u8]>) {
        Digest::update(self, bytes);
    }
}
impl CanonicalSink for Vec<u8> {
    fn write(&mut self, bytes: impl AsRef<[u8]>) {
        self.extend_from_slice(bytes.as_ref());
    }
}
fn put(sink: &mut impl CanonicalSink, value: &str) {
    sink.write((value.len() as u64).to_le_bytes());
    sink.write(value.as_bytes());
}
fn authority_hash(search: &SavedSearch, block: &DaypartBlock, rule: &RecurringRule) -> String {
    let mut digest = Sha256::new();
    encode_source(&mut digest, search, block, rule);
    format!("{:x}", digest.finalize())
}
fn evidence_scope_hash(
    kind: SourceEvidenceKind,
    source_authority_sha256: &str,
    window: &MaterializationWindow,
) -> String {
    let mut digest = Sha256::new();
    put(&mut digest, "townlight.materialization-evidence-scope.v1");
    put(&mut digest, source_evidence_kind_name(kind));
    put(&mut digest, source_authority_sha256);
    digest.write(window.starts_at_utc_ms.to_le_bytes());
    digest.write(window.ends_at_utc_ms.to_le_bytes());
    format!("{:x}", digest.finalize())
}
fn combined_scope_hash(candidate_scope_sha256: &str, prior_airings_scope_sha256: &str) -> String {
    let mut digest = Sha256::new();
    put(&mut digest, "townlight.materialization-evidence-scopes.v1");
    put(&mut digest, candidate_scope_sha256);
    put(&mut digest, prior_airings_scope_sha256);
    format!("{:x}", digest.finalize())
}
fn projection_hash(kind: SourceEvidenceKind, canonical_bytes: &[u8]) -> String {
    let mut digest = Sha256::new();
    put(&mut digest, "townlight.materialization-projection.v1");
    put(&mut digest, source_evidence_kind_name(kind));
    digest.write(canonical_bytes);
    format!("{:x}", digest.finalize())
}
fn receipt_hash(receipt: &SourceEvidenceReceipt) -> String {
    let mut digest = Sha256::new();
    encode_receipt(&mut digest, receipt);
    format!("{:x}", digest.finalize())
}
fn input_binding_hash(binding: &MaterializationInputBinding) -> String {
    let mut digest = Sha256::new();
    encode_input_binding(&mut digest, binding, false);
    format!("{:x}", digest.finalize())
}
fn source_evidence_kind_name(kind: SourceEvidenceKind) -> &'static str {
    match kind {
        SourceEvidenceKind::CandidateProjection => "candidate-projection",
        SourceEvidenceKind::PriorAirings => "prior-airings",
    }
}
fn occurrence_hash(rule_id: &str, channel_id: &str, starts_at_utc_ms: i64) -> String {
    let mut digest = Sha256::new();
    put(&mut digest, "townlight.schedule-occurrence.v2");
    digest.write(OCCURRENCE_SCHEMA_VERSION.to_le_bytes());
    put(&mut digest, rule_id);
    put(&mut digest, channel_id);
    digest.write(starts_at_utc_ms.to_le_bytes());
    format!("{:x}", digest.finalize())
}

fn selection_seed(rule_id: &str, channel_id: &str, starts_at_utc_ms: i64) -> u64 {
    let mut digest = Sha256::new();
    put(&mut digest, "townlight.schedule-pick-seed.v2");
    put(&mut digest, rule_id);
    put(&mut digest, channel_id);
    digest.write(starts_at_utc_ms.to_le_bytes());
    let bytes = digest.finalize();
    u64::from_le_bytes(
        bytes[..8]
            .try_into()
            .expect("SHA-256 prefix has eight bytes"),
    )
}
pub fn canonical_source_bytes(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
) -> Result<Vec<u8>, String> {
    rule.validate_against(search, block)?;
    let mut bytes = Vec::new();
    encode_source(&mut bytes, search, block, rule);
    Ok(bytes)
}
/// Returns the scope that an external catalog or as-run authority must bind in
/// its receipt before this crate will consume that projection.
pub fn materialization_evidence_scope_sha256(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
    window: &MaterializationWindow,
    kind: SourceEvidenceKind,
) -> Result<String, String> {
    rule.validate_against(search, block)?;
    window.validate()?;
    Ok(evidence_scope_hash(
        kind,
        &authority_hash(search, block, rule),
        window,
    ))
}
fn encode_source(
    digest: &mut impl CanonicalSink,
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
) {
    put(digest, "townlight.schedule-source.v2");
    digest.write(SOURCE_AUTHORITY_SCHEMA_VERSION.to_le_bytes());
    for value in [
        &search.search_id,
        &search.channel_id,
        &search.owner_operator_id,
    ] {
        put(digest, value);
    }
    digest.write(search.revision.to_le_bytes());
    encode_query(digest, &search.query);
    for value in [&block.block_id, &block.channel_id, &block.label] {
        put(digest, value);
    }
    digest.write(block.starts_local_minute.to_le_bytes());
    digest.write(block.ends_local_minute.to_le_bytes());
    digest.write([block.weekday_mask]);
    match (&block.season_starts_on, &block.season_ends_on) {
        (Some(start), Some(end)) => {
            digest.write([1]);
            encode_date(digest, start);
            encode_date(digest, end);
        }
        _ => digest.write([0]),
    }
    digest.write((block.allow_rule_ids.len() as u32).to_le_bytes());
    for rule_id in &block.allow_rule_ids {
        put(digest, rule_id);
    }
    digest.write(block.revision.to_le_bytes());
    for value in [
        &rule.rule_id,
        &rule.channel_id,
        &rule.saved_search_id,
        &rule.block_id,
    ] {
        put(digest, value);
    }
    digest.write(rule.saved_search_revision.to_le_bytes());
    digest.write(rule.block_revision.to_le_bytes());
    digest.write([match rule.pick_strategy {
        AssetPickStrategy::Top => 0,
        AssetPickStrategy::Random => 1,
        AssetPickStrategy::Newest => 2,
        AssetPickStrategy::LeastRecentlyAired => 3,
    }]);
    digest.write([rule.rolling_window_days]);
    digest.write([rule.avoid_repeat_within_days.unwrap_or(0)]);
    digest.write([u8::from(rule.enabled)]);
    digest.write(rule.priority.to_le_bytes());
    digest.write([rule.weekday_mask, rule.interval_weeks]);
    digest.write(rule.anchor_date.year.to_le_bytes());
    digest.write([rule.anchor_date.month, rule.anchor_date.day]);
    match &rule.timezone {
        RecurrenceTimeZone::Utc => put(digest, "utc"),
        RecurrenceTimeZone::Named { iana_name, dst } => {
            put(digest, "named");
            put(digest, iana_name);
            put(
                digest,
                match dst {
                    DstApplication::EarlierFold => "earlier",
                    DstApplication::LaterFold => "later",
                    DstApplication::Standard => "invalid",
                },
            );
        }
    }
    digest.write(rule.local_start_minute.to_le_bytes());
    digest.write(rule.duration_minutes.to_le_bytes());
    digest.write(rule.revision.to_le_bytes());
}
fn intent_hash(intent: &MaterializationIntent) -> String {
    let mut digest = Sha256::new();
    encode_intent(&mut digest, intent);
    format!("{:x}", digest.finalize())
}
pub fn canonical_intent_bytes(intent: &MaterializationIntent) -> Vec<u8> {
    let mut bytes = Vec::new();
    encode_intent(&mut bytes, intent);
    bytes
}
fn encode_intent(digest: &mut impl CanonicalSink, intent: &MaterializationIntent) {
    put(digest, "townlight.materialization-intent.v3");
    digest.write(intent.hash_schema_version.to_le_bytes());
    for value in [&intent.rule_id, &intent.channel_id, &intent.block_id] {
        put(digest, value);
    }
    digest.write(intent.rule_revision.to_le_bytes());
    digest.write(intent.block_revision.to_le_bytes());
    digest.write(intent.window.starts_at_utc_ms.to_le_bytes());
    digest.write(intent.window.ends_at_utc_ms.to_le_bytes());
    put(digest, &intent.source_authority_sha256);
    encode_input_binding(digest, &intent.source_input_binding, true);
    digest.write((intent.slots.len() as u32).to_le_bytes());
    for slot in &intent.slots {
        put(digest, &slot.occurrence_id);
        put(digest, &slot.asset_sha256);
        put(digest, &slot.asset_title);
        digest.write(slot.local_date.year.to_le_bytes());
        digest.write([slot.local_date.month, slot.local_date.day]);
        digest.write(slot.local_minute.to_le_bytes());
        put(digest, &slot.iana_zone);
        digest.write(slot.utc_offset_seconds.to_le_bytes());
        digest.write(slot.starts_at_utc_ms.to_le_bytes());
        digest.write(slot.ends_at_utc_ms.to_le_bytes());
        digest.write([match slot.resolution {
            LocalResolution::Normal => 0,
            LocalResolution::EarlierFold => 1,
            LocalResolution::LaterFold => 2,
            LocalResolution::Gap => 3,
        }]);
    }
}
/// Canonical, ordered candidate bytes. The authenticated source boundary must
/// sign the digest of precisely these bytes, including an empty projection.
pub fn canonical_candidate_projection_bytes(
    candidates: &[AssetCandidate],
) -> Result<Vec<u8>, String> {
    validate_candidate_projection(candidates)?;
    let mut bytes = Vec::new();
    put(&mut bytes, "townlight.candidate-projection.v1");
    bytes.write((candidates.len() as u32).to_le_bytes());
    for candidate in candidates {
        put(&mut bytes, &candidate.asset_sha256);
        put(&mut bytes, &candidate.title);
        bytes.write(candidate.duration_ms.to_le_bytes());
        bytes.write([u8::from(candidate.ready), u8::from(candidate.published)]);
        bytes.write(candidate.published_at_utc_ms.to_le_bytes());
        bytes.write((candidate.indexed_custom_metadata.len() as u32).to_le_bytes());
        for (field, value) in &candidate.indexed_custom_metadata {
            put(&mut bytes, field);
            encode_metadata_value(&mut bytes, value);
        }
    }
    Ok(bytes)
}

/// Canonical, ordered as-run bytes. An empty sequence is not a no-airings
/// claim unless a verifier accepts the receipt for its exact digest.
pub fn canonical_prior_airings_bytes(airings: &[PriorAiring]) -> Result<Vec<u8>, String> {
    validate_prior_airings_projection(airings)?;
    let mut bytes = Vec::new();
    put(&mut bytes, "townlight.prior-airings-projection.v1");
    bytes.write((airings.len() as u32).to_le_bytes());
    for airing in airings {
        put(&mut bytes, &airing.asset_sha256);
        bytes.write(airing.aired_at_utc_ms.to_le_bytes());
    }
    Ok(bytes)
}

pub fn canonical_unverified_input_binding_bytes(
    binding: &MaterializationInputBinding,
) -> Result<Vec<u8>, String> {
    if [
        &binding.candidate_projection_sha256,
        &binding.candidate_receipt_sha256,
        &binding.prior_airings_sha256,
        &binding.prior_airings_receipt_sha256,
        &binding.scope_sha256,
        &binding.combined_sha256,
    ]
    .iter()
    .any(|value| !lowercase_sha256(value))
        || binding.combined_sha256 != input_binding_hash(binding)
    {
        return Err("materialization input binding is malformed".into());
    }
    let mut bytes = Vec::new();
    encode_input_binding(&mut bytes, binding, true);
    Ok(bytes)
}

fn validate_candidate_projection(candidates: &[AssetCandidate]) -> Result<(), String> {
    if candidates.len() > MAX_QUERY_LIMIT {
        return Err("candidate projection is unbounded".into());
    }
    for candidate in candidates {
        candidate.validate()?;
    }
    if candidates
        .windows(2)
        .any(|pair| pair[0].asset_sha256 >= pair[1].asset_sha256)
    {
        return Err("candidate projection is unordered or duplicate".into());
    }
    Ok(())
}

fn validate_prior_airings_projection(airings: &[PriorAiring]) -> Result<(), String> {
    if airings.len() > MAX_QUERY_LIMIT {
        return Err("prior-airings projection is unbounded".into());
    }
    for airing in airings {
        if !lowercase_sha256(&airing.asset_sha256) || airing.aired_at_utc_ms < 0 {
            return Err("prior-airings projection is malformed".into());
        }
    }
    if airings.windows(2).any(|pair| {
        (&pair[0].asset_sha256, pair[0].aired_at_utc_ms)
            >= (&pair[1].asset_sha256, pair[1].aired_at_utc_ms)
    }) {
        return Err("prior-airings projection is unordered or duplicate".into());
    }
    Ok(())
}

fn encode_receipt(digest: &mut impl CanonicalSink, receipt: &SourceEvidenceReceipt) {
    put(digest, "townlight.materialization-evidence-receipt.v1");
    for value in [
        &receipt.issuer_id,
        &receipt.receipt_id,
        &receipt.scope_sha256,
        &receipt.attestation_sha256,
    ] {
        put(digest, value);
    }
    digest.write(receipt.issued_at_utc_ms.to_le_bytes());
    digest.write(receipt.expires_at_utc_ms.to_le_bytes());
}

fn encode_input_binding(
    digest: &mut impl CanonicalSink,
    binding: &MaterializationInputBinding,
    include_combined_sha256: bool,
) {
    put(digest, "townlight.materialization-input-binding.v1");
    for value in [
        &binding.candidate_projection_sha256,
        &binding.candidate_receipt_sha256,
        &binding.prior_airings_sha256,
        &binding.prior_airings_receipt_sha256,
        &binding.scope_sha256,
    ] {
        put(digest, value);
    }
    if include_combined_sha256 {
        put(digest, &binding.combined_sha256);
    }
}
fn encode_query(digest: &mut impl CanonicalSink, query: &SavedSearchQuery) {
    digest.write([match query.order_by {
        SavedSearchOrder::TitleAscending => 0,
        SavedSearchOrder::PublishedNewest => 1,
    }]);
    digest.write(query.limit.to_le_bytes());
    for filters in [&query.all_of, &query.any_of] {
        digest.write((filters.len() as u32).to_le_bytes());
        for filter in filters {
            put(digest, &filter.field);
            encode_metadata_value(digest, &filter.value);
        }
    }
}
fn encode_date(digest: &mut impl CanonicalSink, value: &LocalDate) {
    digest.write(value.year.to_le_bytes());
    digest.write([value.month, value.day]);
}
fn encode_metadata_value(digest: &mut impl CanonicalSink, value: &MetadataValue) {
    match value {
        MetadataValue::Text(value) => {
            digest.write([0]);
            put(digest, value);
        }
        MetadataValue::Number(value) => {
            digest.write([1]);
            digest.write(value.to_le_bytes());
        }
        MetadataValue::Boolean(value) => digest.write([2, u8::from(*value)]),
        MetadataValue::DateTimeUnixMs(value) => {
            digest.write([3]);
            digest.write(value.to_le_bytes());
        }
        MetadataValue::Choice(value) => {
            digest.write([4]);
            put(digest, value);
        }
    }
}
