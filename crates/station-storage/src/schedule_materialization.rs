use std::collections::{BTreeMap, BTreeSet};

#[cfg(test)]
use std::cell::Cell;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_domain::AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID;
use station_schedule::{ScheduleItem, ScheduleState};
use station_schedule_automation::{
    ArbitratedMaterializationBatch, ArbitrationPrecedence, AuthenticatedCandidateProjection,
    AuthenticatedMaterializationInputs, AuthenticatedPriorAirings, DaypartBlock, MAX_PRIOR_AIRINGS,
    MaterializationAuthorities, MaterializationPlan, MaterializationWindow, PriorAiring,
    RecurringRule, SavedSearch, SlotMaterializationOutcome, SourceEvidenceAuthority,
    SourceEvidenceKind, WindowedBatchMaterializationRequest, canonical_prior_airings_sha256,
    derive_arbitrated_materialization_batch_for_windows, materialization_evidence_scope_sha256,
    materialization_prior_airings_coverage_window,
};

use super::*;

const MAX_BATCH_REQUESTS: usize = 64;
const MAX_BATCH_OUTCOMES: usize = 4_096;
const MAX_AUTOMATION_DRAFTS_PER_CHANNEL: usize = 4_096;
const MAX_AUTOMATIC_MATERIALIZATION_CHANNELS: usize = 256;
const MAX_AUTOMATIC_RECURRING_RULE_SCAN: usize =
    MAX_AUTOMATIC_MATERIALIZATION_CHANNELS * MAX_BATCH_REQUESTS;
/// The public run-history read boundary accepts at most this many runs at a
/// time. The page is deliberately keyset-paginated rather than offset based so
/// concurrent newer runs cannot make an operator skip or duplicate evidence.
pub const MAX_SCHEDULE_MATERIALIZATION_HISTORY_PAGE_SIZE: usize = 128;
const MAX_SCHEDULE_MATERIALIZATION_HISTORY_OUTCOMES: usize = MAX_BATCH_OUTCOMES;
const MAX_SCHEDULE_MATERIALIZATION_HISTORY_DRAFTS: usize = MAX_BATCH_OUTCOMES;
const MATERIALIZATION_RECEIPT_LIFETIME_MS: i64 = 60_000;
const UTC_MINUTE_MS: i64 = 60_000;
const UTC_DAY_MS: i64 = 86_400_000;

#[cfg(test)]
thread_local! {
    /// Test-only instrumentation. A thread-local counter keeps unrelated
    /// parallel test cases from masking an accidental per-Draft full-run read.
    static MATERIALIZATION_RUN_FULL_READS: Cell<usize> = const { Cell::new(0) };
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationSourceSnapshot {
    pub search: SavedSearch,
    pub block: DaypartBlock,
    pub rule: RecurringRule,
    pub candidate_projection: CatalogCandidateProjection,
    pub prior_airings: Vec<PriorAiring>,
    pub prior_airings_sha256: String,
    pub prior_airings_coverage: MaterializationWindow,
    pub window: MaterializationWindow,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ScheduleMaterializationOutcome {
    Proposed {
        occurrence_id: String,
        schedule_item_id: String,
        asset_sha256: String,
        asset_title: String,
        starts_at_utc_ms: i64,
        ends_at_utc_ms: i64,
    },
    NoMatch {
        starts_at_utc_ms: i64,
    },
    Blocked {
        starts_at_utc_ms: i64,
    },
    SuppressedByPrecedence {
        starts_at_utc_ms: i64,
        winner_rule_id: String,
        candidate_priority: i16,
        winner_priority: i16,
        reason: String,
    },
    ExistingAutomationDraftUnchanged {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
    },
    ExistingAutomationDraftChanged {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
    },
    ExistingCommitted {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
    },
    CancelledOccurrence {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
    },
    AlreadyAired {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
    },
    SuppressedByExistingSchedule {
        occurrence_id: String,
        schedule_item_id: String,
        existing: ExistingScheduleSnapshot,
        existing_owner: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExistingScheduleSnapshot {
    pub schedule_item_id: String,
    pub revision: u64,
    pub channel_id: String,
    pub asset_sha256: String,
    pub title: String,
    pub starts_at_utc_ms: i64,
    pub duration_ms: u64,
    pub state: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleMaterializationOutcomeRecord {
    pub ordinal: u32,
    pub rule_id: String,
    pub rule_revision: u64,
    pub channel_id: String,
    pub saved_search_id: String,
    pub saved_search_revision: u64,
    pub block_id: String,
    pub block_revision: u64,
    pub plan_window: MaterializationWindow,
    pub materialization_id: Option<String>,
    pub source_authority_sha256: String,
    pub candidate_projection_sha256: String,
    pub prior_airings_sha256: String,
    pub prior_airings_coverage: MaterializationWindow,
    pub scope_sha256: String,
    pub source_input_sha256: String,
    pub compiled_outcome: ScheduleMaterializationOutcome,
    pub outcome: ScheduleMaterializationOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationRun {
    pub run_id: String,
    pub request_sha256: String,
    pub result_sha256: String,
    pub batch_sha256: String,
    pub window: MaterializationWindow,
    pub observed_at_utc_ms: i64,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
    pub plans: Vec<ScheduleMaterializationPlanRecord>,
    pub outcomes: Vec<ScheduleMaterializationOutcomeRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomaticMaterializationChannelCheckpoint {
    pub channel_id: String,
    pub latest_run: Option<ScheduleMaterializationRun>,
}

/// An exclusive keyset cursor for channel-scoped materialization-run history.
/// Entries are ordered descending by `(occurred_at_unix_ms, run_id)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationHistoryCursor {
    pub occurred_at_unix_ms: i64,
    pub run_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScheduleMaterializationRunOrigin {
    /// The immutable actor is the reserved domain-owned supervisor identity.
    /// Operator-account creation and migration reject that identity, making this
    /// comparison the stable automatic-vs-operator trust boundary rather than a
    /// caller-supplied label.
    Automatic,
    /// Every other valid immutable attribution is an operator-originated run.
    Operator,
}

/// A Draft created by the selected run. `current_schedule` is intentionally
/// current (rather than a historical snapshot): the immutable provenance is
/// verified against the run while the operator can see the Draft's present
/// revision and state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationGeneratedDraft {
    pub occurrence_id: String,
    pub schedule_item_id: String,
    pub current_schedule: ScheduleItem,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationHistoryEntry {
    pub run: ScheduleMaterializationRun,
    pub origin: ScheduleMaterializationRunOrigin,
    pub generated_drafts: Vec<ScheduleMaterializationGeneratedDraft>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleMaterializationHistoryPage {
    pub entries: Vec<ScheduleMaterializationHistoryEntry>,
    pub next_cursor: Option<ScheduleMaterializationHistoryCursor>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleMaterializationHistoryReadError {
    Invalid(String),
    Capacity,
    Integrity(String),
    Storage(String),
}

/// Internal result for the immutable run verifier. Persisted contradictions
/// must remain distinct from SQLite failures so the public history boundary
/// does not misreport a damaged station database as an integrity finding.
#[derive(Debug)]
enum VerifiedMaterializationRunReadError {
    Integrity(String),
    Storage(String),
}

impl From<String> for VerifiedMaterializationRunReadError {
    fn from(error: String) -> Self {
        Self::Integrity(error)
    }
}

impl From<&str> for VerifiedMaterializationRunReadError {
    fn from(error: &str) -> Self {
        Self::Integrity(error.into())
    }
}

impl VerifiedMaterializationRunReadError {
    fn into_string(self) -> String {
        match self {
            Self::Integrity(error) | Self::Storage(error) => error,
        }
    }

    fn into_history_error(self) -> ScheduleMaterializationHistoryReadError {
        match self {
            Self::Integrity(error) => ScheduleMaterializationHistoryReadError::Integrity(error),
            Self::Storage(error) => ScheduleMaterializationHistoryReadError::Storage(error),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleMaterializationPlanRecord {
    pub ordinal: u32,
    pub rule_id: String,
    pub rule_revision: u64,
    pub channel_id: String,
    pub saved_search_id: String,
    pub saved_search_revision: u64,
    pub block_id: String,
    pub block_revision: u64,
    pub window: MaterializationWindow,
    pub source_authority_sha256: String,
    pub candidate_projection_sha256: String,
    pub candidate_receipt_sha256: String,
    pub prior_airings_sha256: String,
    pub prior_receipt_sha256: String,
    pub prior_airings_coverage: MaterializationWindow,
    pub scope_sha256: String,
    pub source_input_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleMaterializationApplyOutcome {
    Applied(ScheduleMaterializationRun),
    AlreadyApplied(ScheduleMaterializationRun),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleMaterializationWriteError {
    Invalid(String),
    StaleAuthority(String),
    Collision(String),
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScheduleMaterializationFailureClass {
    ChannelDisabled,
    StaleAuthority,
    InvalidDerivedInput,
    Capacity,
    Integrity,
    Storage,
}

#[derive(Serialize)]
struct BatchDigest<'a> {
    schema: &'static str,
    window_starts_at_utc_ms: i64,
    window_ends_at_utc_ms: i64,
    plans: &'a [PlanDigest],
    outcomes: &'a [SemanticOutcomeDigest],
}

#[derive(Serialize)]
struct ResultDigest<'a> {
    schema: &'static str,
    request_sha256: &'a str,
    outcomes: &'a [ResultOutcomeDigest],
}

#[derive(Serialize)]
struct ResultOutcomeDigest {
    ordinal: u32,
    rule_id: String,
    rule_revision: u64,
    channel_id: String,
    block_id: String,
    block_revision: u64,
    source_authority_sha256: String,
    compiled_outcome: ScheduleMaterializationOutcome,
    outcome: ScheduleMaterializationOutcome,
}

#[derive(Serialize)]
struct PlanDigest {
    rule_id: String,
    rule_revision: u64,
    channel_id: String,
    saved_search_id: String,
    saved_search_revision: u64,
    block_id: String,
    block_revision: u64,
    window_starts_at_utc_ms: i64,
    window_ends_at_utc_ms: i64,
    source_authority_sha256: String,
    candidate_projection_sha256: String,
    prior_airings_sha256: String,
    prior_coverage_starts_at_utc_ms: i64,
    prior_coverage_ends_at_utc_ms: i64,
    scope_sha256: String,
}

#[derive(Serialize)]
struct SemanticOutcomeDigest {
    ordinal: u32,
    rule_id: String,
    rule_revision: u64,
    channel_id: String,
    block_id: String,
    block_revision: u64,
    source_authority_sha256: String,
    outcome: ScheduleMaterializationOutcome,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ScheduleMaterializationOrigin {
    occurrence_id: String,
    schedule_item_id: String,
    channel_id: String,
    saved_search_id: String,
    saved_search_revision: u64,
    rule_id: String,
    rule_revision: u64,
    block_id: String,
    block_revision: u64,
    plan_window: MaterializationWindow,
    materialization_id: String,
    source_authority_sha256: String,
    source_input_sha256: String,
    candidate_projection_sha256: String,
    prior_airings_sha256: String,
    prior_airings_coverage: MaterializationWindow,
    scope_sha256: String,
    original_schedule: ExistingScheduleSnapshot,
    run_id: String,
}

impl StationStore {
    pub(super) fn initialize_schedule_materialization_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS schedule_materialization_runs (\
               run_id TEXT PRIMARY KEY,\
               request_sha256 TEXT NOT NULL,\
               result_sha256 TEXT NOT NULL UNIQUE,\
               window_starts_at_utc_ms INTEGER NOT NULL CHECK (window_starts_at_utc_ms >= 0),\
               window_ends_at_utc_ms INTEGER NOT NULL CHECK (window_ends_at_utc_ms > window_starts_at_utc_ms),\
               observed_at_utc_ms INTEGER NOT NULL CHECK (observed_at_utc_ms >= 0),\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms > 0),\
               plan_count INTEGER NOT NULL CHECK (plan_count BETWEEN 1 AND 64),\
               outcome_count INTEGER NOT NULL CHECK (outcome_count BETWEEN 0 AND 4096)\
             );\
             CREATE TABLE IF NOT EXISTS schedule_materialization_plans (\
               run_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 63),\
               rule_id TEXT NOT NULL, channel_id TEXT NOT NULL, record_json TEXT NOT NULL,\
               PRIMARY KEY(run_id, ordinal), FOREIGN KEY(run_id) REFERENCES schedule_materialization_runs(run_id)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_materialization_plans_channel \
               ON schedule_materialization_plans(channel_id,run_id,ordinal);\
             CREATE TABLE IF NOT EXISTS schedule_materialization_outcomes (\
               run_id TEXT NOT NULL,\
               ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 4095),\
               rule_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               record_json TEXT NOT NULL,\
               PRIMARY KEY(run_id, ordinal),\
               FOREIGN KEY(run_id) REFERENCES schedule_materialization_runs(run_id)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_materialization_outcomes_rule \
               ON schedule_materialization_outcomes(rule_id, run_id, ordinal);\
             CREATE INDEX IF NOT EXISTS schedule_materialization_outcomes_channel \
               ON schedule_materialization_outcomes(channel_id, run_id, ordinal);\
             CREATE TABLE IF NOT EXISTS schedule_materialization_run_channels (\
               run_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms > 0),\
               FOREIGN KEY(run_id) REFERENCES schedule_materialization_runs(run_id)\
             );\
             INSERT INTO schedule_materialization_run_channels(run_id,channel_id,occurred_at_unix_ms) \
               SELECT r.run_id,MIN(p.channel_id),r.occurred_at_unix_ms \
               FROM schedule_materialization_runs r JOIN schedule_materialization_plans p ON p.run_id=r.run_id \
               WHERE NOT EXISTS (SELECT 1 FROM schedule_materialization_run_channels b WHERE b.run_id=r.run_id) \
               GROUP BY r.run_id,r.plan_count,r.occurred_at_unix_ms \
               HAVING COUNT(*)=r.plan_count AND COUNT(DISTINCT p.channel_id)=1;\
             CREATE INDEX IF NOT EXISTS schedule_materialization_run_channels_latest \
               ON schedule_materialization_run_channels(channel_id,occurred_at_unix_ms DESC,run_id DESC);\
             CREATE TABLE IF NOT EXISTS schedule_materialization_drafts (\
               schedule_item_id TEXT PRIMARY KEY,\
               run_id TEXT NOT NULL,\
               occurrence_id TEXT NOT NULL UNIQUE,\
               channel_id TEXT NOT NULL,\
               saved_search_id TEXT NOT NULL,\
               saved_search_revision INTEGER NOT NULL CHECK (saved_search_revision >= 1),\
               rule_id TEXT NOT NULL,\
               rule_revision INTEGER NOT NULL CHECK (rule_revision >= 1),\
               block_id TEXT NOT NULL,\
               block_revision INTEGER NOT NULL CHECK (block_revision >= 1),\
               plan_starts_at_utc_ms INTEGER NOT NULL CHECK (plan_starts_at_utc_ms >= 0),\
               plan_ends_at_utc_ms INTEGER NOT NULL CHECK (plan_ends_at_utc_ms > plan_starts_at_utc_ms),\
               materialization_id TEXT NOT NULL,\
               source_authority_sha256 TEXT NOT NULL,\
               source_input_sha256 TEXT NOT NULL,\
               candidate_projection_sha256 TEXT NOT NULL,\
               prior_airings_sha256 TEXT NOT NULL,\
               prior_coverage_starts_at_utc_ms INTEGER NOT NULL CHECK (prior_coverage_starts_at_utc_ms >= 0),\
               prior_coverage_ends_at_utc_ms INTEGER NOT NULL CHECK (prior_coverage_ends_at_utc_ms > prior_coverage_starts_at_utc_ms),\
               scope_sha256 TEXT NOT NULL,\
               original_revision INTEGER NOT NULL CHECK (original_revision >= 1),\
               original_asset_sha256 TEXT NOT NULL,\
               original_title TEXT NOT NULL,\
               original_starts_at_utc_ms INTEGER NOT NULL CHECK (original_starts_at_utc_ms >= 0),\
               original_duration_ms INTEGER NOT NULL CHECK (original_duration_ms > 0),\
               original_state TEXT NOT NULL CHECK (original_state='draft'),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(schedule_item_id) REFERENCES schedule_items(item_id),\
               FOREIGN KEY(run_id) REFERENCES schedule_materialization_runs(run_id)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_materialization_drafts_channel \
               ON schedule_materialization_drafts(channel_id, schedule_item_id);\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_runs_no_update BEFORE UPDATE ON schedule_materialization_runs BEGIN SELECT RAISE(ABORT, 'materialization runs are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_runs_no_delete BEFORE DELETE ON schedule_materialization_runs BEGIN SELECT RAISE(ABORT, 'materialization runs are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_plans_no_update BEFORE UPDATE ON schedule_materialization_plans BEGIN SELECT RAISE(ABORT, 'materialization plans are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_plans_no_delete BEFORE DELETE ON schedule_materialization_plans BEGIN SELECT RAISE(ABORT, 'materialization plans are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_outcomes_no_update BEFORE UPDATE ON schedule_materialization_outcomes BEGIN SELECT RAISE(ABORT, 'materialization outcomes are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_outcomes_no_delete BEFORE DELETE ON schedule_materialization_outcomes BEGIN SELECT RAISE(ABORT, 'materialization outcomes are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_run_channels_no_update BEFORE UPDATE ON schedule_materialization_run_channels BEGIN SELECT RAISE(ABORT, 'materialization run channel bindings are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_run_channels_no_delete BEFORE DELETE ON schedule_materialization_run_channels BEGIN SELECT RAISE(ABORT, 'materialization run channel bindings are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_drafts_no_update BEFORE UPDATE ON schedule_materialization_drafts BEGIN SELECT RAISE(ABORT, 'materialization draft provenance is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_materialization_drafts_no_delete BEFORE DELETE ON schedule_materialization_drafts BEGIN SELECT RAISE(ABORT, 'materialization draft provenance is append-only'); END;",
        )?;
        self.audit_materialization_run_channel_bindings()
    }

    /// Backfill can derive a historical binding only when its plan authority is
    /// complete and single-channel. Any older run for which that is not true is
    /// unsafe to hide from channel history, so opening the station fails closed
    /// instead of serving a page that silently falls back to an older run.
    fn audit_materialization_run_channel_bindings(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT r.run_id,r.occurred_at_unix_ms,r.plan_count,b.channel_id,b.occurred_at_unix_ms, \
                    COUNT(DISTINCT b.run_id),COUNT(p.run_id),MIN(p.channel_id),MAX(p.channel_id) \
             FROM schedule_materialization_runs r \
             LEFT JOIN schedule_materialization_run_channels b ON b.run_id=r.run_id \
             LEFT JOIN schedule_materialization_plans p ON p.run_id=r.run_id \
             GROUP BY r.run_id,r.occurred_at_unix_ms,b.channel_id,b.occurred_at_unix_ms \
             ORDER BY r.run_id",
        )?;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let run_id = column_text(statement.raw, 0)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 1) };
                    let expected_plan_count = unsafe { sqlite3_column_int64(statement.raw, 2) };
                    let binding_channel_id = column_text(statement.raw, 3)?;
                    let binding_occurred_at_unix_ms =
                        unsafe { sqlite3_column_int64(statement.raw, 4) };
                    let binding_count = unsafe { sqlite3_column_int64(statement.raw, 5) };
                    let plan_count = unsafe { sqlite3_column_int64(statement.raw, 6) };
                    let minimum_plan_channel = if plan_count == 0 {
                        None
                    } else {
                        Some(column_text(statement.raw, 7)?)
                    };
                    let maximum_plan_channel = if plan_count == 0 {
                        None
                    } else {
                        Some(column_text(statement.raw, 8)?)
                    };
                    if !is_canonical_uuid(&run_id)
                        || occurred_at_unix_ms <= 0
                        || expected_plan_count < 0
                        || plan_count != expected_plan_count
                        || binding_count != 1
                        || !is_canonical_uuid(&binding_channel_id)
                        || binding_occurred_at_unix_ms != occurred_at_unix_ms
                        || minimum_plan_channel
                            .as_deref()
                            .is_some_and(|channel| channel != binding_channel_id)
                        || maximum_plan_channel
                            .as_deref()
                            .is_some_and(|channel| channel != binding_channel_id)
                    {
                        return Err(
                            "materialization run channel binding migration is incomplete or contradictory"
                                .into(),
                        );
                    }
                }
                SQLITE_DONE => return Ok(()),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    /// Trusted in-process/testing seam for an explicit window. Never bind this
    /// method to an HTTP or other remotely caller-controlled boundary.
    pub fn materialize_schedule_batch_with_explicit_window(
        &self,
        channel_id: &str,
        window: &MaterializationWindow,
        changed_by: &str,
        authority: &impl SourceEvidenceAuthority,
    ) -> Result<ScheduleMaterializationApplyOutcome, ScheduleMaterializationWriteError> {
        if !is_canonical_uuid(channel_id) || !is_canonical_uuid(changed_by) {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization request or attribution is invalid or unbounded".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let outcome = self.materialize_schedule_batch_in_transaction(
            channel_id,
            Some(window),
            changed_by,
            authority,
        );
        finish_materialization_transaction(self, outcome)
    }

    pub fn materialize_scheduled_channel(
        &self,
        channel_id: &str,
        changed_by: &str,
        authority: &impl SourceEvidenceAuthority,
    ) -> Result<ScheduleMaterializationApplyOutcome, ScheduleMaterializationWriteError> {
        if !is_canonical_uuid(channel_id) || !is_canonical_uuid(changed_by) {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization request or attribution is invalid or unbounded".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let outcome = (|| {
            if self
                .read_materialization_channel_enabled_locked(channel_id)
                .map_err(ScheduleMaterializationWriteError::Storage)?
                != Some(true)
            {
                return Err(ScheduleMaterializationWriteError::StaleAuthority(
                    "materialization channel is missing or disabled".into(),
                ));
            }
            self.materialize_schedule_batch_in_transaction(channel_id, None, changed_by, authority)
        })();
        finish_materialization_transaction(self, outcome)
    }

    pub fn list_automatic_materialization_channel_checkpoints(
        &self,
    ) -> Result<Vec<AutomaticMaterializationChannelCheckpoint>, String> {
        self.execute("BEGIN DEFERRED")?;
        let mut checkpoints = None;
        let outcome = (|| {
            let now_utc_ms = station_clock_utc_ms()?;
            let statement = self.prepare(
                "SELECT rule_id FROM schedule_automation_recurring_rules ORDER BY rule_id LIMIT ?1",
            )?;
            bind_i64(
                self.database,
                statement.raw,
                1,
                i64::try_from(MAX_AUTOMATIC_RECURRING_RULE_SCAN + 1)
                    .map_err(|_| "automatic materialization rule bound overflowed".to_string())?,
            )?;
            let mut rule_ids = Vec::new();
            loop {
                match unsafe { sqlite3_step(statement.raw) } {
                    SQLITE_ROW => rule_ids.push(column_text(statement.raw, 0)?),
                    SQLITE_DONE => break,
                    _ => return Err(sqlite_error(self.database)),
                }
            }
            if rule_ids.len() > MAX_AUTOMATIC_RECURRING_RULE_SCAN {
                return Err("automatic materialization recurring-rule capacity exceeded".into());
            }
            drop(statement);

            let mut eligible_channels = BTreeSet::new();
            for rule_id in rule_ids {
                let rule = self.read_recurring_rule(&rule_id)?.ok_or_else(|| {
                    "automatic materialization recurring rule disappeared".to_string()
                })?;
                if rule.enabled
                    && self.read_materialization_channel_enabled_locked(&rule.channel_id)?
                        == Some(true)
                {
                    eligible_channels.insert(rule.channel_id);
                }
            }
            if eligible_channels.len() > MAX_AUTOMATIC_MATERIALIZATION_CHANNELS {
                return Err("automatic materialization channel capacity exceeded".into());
            }
            let mut resolved = Vec::with_capacity(eligible_channels.len());
            for channel_id in eligible_channels {
                let latest = self.latest_materialization_run_binding_locked(&channel_id)?;
                let plan_latest = self.latest_materialization_run_plan_locked(&channel_id)?;
                if latest != plan_latest {
                    return Err(
                        "automatic materialization newest channel checkpoint contradicts plan authority"
                            .into(),
                    );
                }
                let latest_run = match latest {
                    None => None,
                    Some((run_id, bound_occurred_at_unix_ms)) => {
                        let run = self
                            .read_schedule_materialization_run(&run_id)?
                            .ok_or_else(|| {
                                "materialization checkpoint run disappeared".to_string()
                            })?;
                        if run.occurred_at_unix_ms != bound_occurred_at_unix_ms
                            || run.occurred_at_unix_ms > now_utc_ms
                            || run.observed_at_utc_ms > now_utc_ms
                            || run.plans.iter().any(|plan| plan.channel_id != channel_id)
                            || run
                                .outcomes
                                .iter()
                                .any(|record| record.channel_id != channel_id)
                        {
                            return Err(
                                "automatic materialization checkpoint authority is invalid".into(),
                            );
                        }
                        let plan_channels = run
                            .plans
                            .iter()
                            .map(|plan| plan.channel_id.as_str())
                            .collect::<BTreeSet<_>>();
                        if plan_channels.len() != 1 || !plan_channels.contains(channel_id.as_str())
                        {
                            return Err(
                                "automatic materialization checkpoint has mixed channel plans"
                                    .into(),
                            );
                        }
                        Some(run)
                    }
                };
                resolved.push(AutomaticMaterializationChannelCheckpoint {
                    channel_id,
                    latest_run,
                });
            }
            checkpoints = Some(resolved);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        checkpoints
            .ok_or_else(|| "automatic materialization checkpoint read produced no result".into())
    }

    /// Reads a bounded, channel-scoped page of durable materialization runs.
    ///
    /// This is an evidence boundary, not a best-effort activity feed. One
    /// deferred SQLite snapshot supplies the page and every selected run is
    /// re-read through the full immutable-run verifier. A malformed newest row,
    /// channel binding, or Draft provenance record therefore fails the request;
    /// it is never skipped in favor of an older row or represented by a partial
    /// page.
    pub fn list_schedule_materialization_history(
        &self,
        channel_id: &str,
        limit: usize,
        cursor: Option<&ScheduleMaterializationHistoryCursor>,
    ) -> Result<ScheduleMaterializationHistoryPage, ScheduleMaterializationHistoryReadError> {
        if !is_canonical_uuid(channel_id) {
            return Err(ScheduleMaterializationHistoryReadError::Invalid(
                "materialization history channel identity is invalid".into(),
            ));
        }
        if !(1..=MAX_SCHEDULE_MATERIALIZATION_HISTORY_PAGE_SIZE).contains(&limit) {
            return Err(ScheduleMaterializationHistoryReadError::Invalid(
                "materialization history page limit is invalid or unbounded".into(),
            ));
        }
        if let Some(cursor) = cursor
            && (cursor.occurred_at_unix_ms <= 0 || !is_canonical_uuid(&cursor.run_id))
        {
            return Err(ScheduleMaterializationHistoryReadError::Invalid(
                "materialization history cursor is invalid".into(),
            ));
        }

        self.execute("BEGIN DEFERRED")
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let outcome = (|| {
            let selected =
                self.list_materialization_history_bindings_locked(channel_id, limit, cursor)?;
            let has_next = selected.len() > limit;
            let mut entries = Vec::with_capacity(selected.len().min(limit));
            let mut total_outcomes = 0_usize;
            let mut total_drafts = 0_usize;

            for (index, binding) in selected.into_iter().enumerate() {
                let entry = self.read_materialization_history_entry_locked(channel_id, &binding)?;
                if index >= limit {
                    // The limit+1 probe is also fully verified before issuing a
                    // cursor. Otherwise a corrupted immediate-next row could be
                    // silently hidden behind an apparently valid current page.
                    continue;
                }
                extend_materialization_history_aggregate(
                    &mut total_outcomes,
                    &mut total_drafts,
                    entry.run.outcomes.len(),
                    entry.generated_drafts.len(),
                )?;
                entries.push(entry);
            }

            let next_cursor = has_next.then(|| entries.last()).flatten().map(|entry| {
                ScheduleMaterializationHistoryCursor {
                    occurred_at_unix_ms: entry.run.occurred_at_unix_ms,
                    run_id: entry.run.run_id.clone(),
                }
            });
            Ok(ScheduleMaterializationHistoryPage {
                entries,
                next_cursor,
            })
        })();
        finish_materialization_history_transaction(self, outcome)
    }

    fn list_materialization_history_bindings_locked(
        &self,
        channel_id: &str,
        limit: usize,
        cursor: Option<&ScheduleMaterializationHistoryCursor>,
    ) -> Result<Vec<ScheduleMaterializationHistoryCursor>, ScheduleMaterializationHistoryReadError>
    {
        let fetch_limit = i64::try_from(
            limit
                .checked_add(1)
                .ok_or(ScheduleMaterializationHistoryReadError::Capacity)?,
        )
        .map_err(|_| ScheduleMaterializationHistoryReadError::Capacity)?;
        let channel = c_string(channel_id, "materialization history channel")
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let statement = match cursor {
            Some(_) => self
                .prepare(
                    "SELECT DISTINCT r.run_id,r.occurred_at_unix_ms \
                 FROM schedule_materialization_runs r \
                 LEFT JOIN schedule_materialization_run_channels b ON b.run_id=r.run_id \
                 LEFT JOIN schedule_materialization_plans p ON p.run_id=r.run_id \
                 WHERE (b.channel_id=?1 OR p.channel_id=?1) \
                   AND (r.occurred_at_unix_ms<?2 \
                     OR (r.occurred_at_unix_ms=?2 AND r.run_id<?3)) \
                 ORDER BY r.occurred_at_unix_ms DESC,r.run_id DESC LIMIT ?4",
                )
                .map_err(ScheduleMaterializationHistoryReadError::Storage)?,
            None => self
                .prepare(
                    "SELECT DISTINCT r.run_id,r.occurred_at_unix_ms \
                 FROM schedule_materialization_runs r \
                 LEFT JOIN schedule_materialization_run_channels b ON b.run_id=r.run_id \
                 LEFT JOIN schedule_materialization_plans p ON p.run_id=r.run_id \
                 WHERE b.channel_id=?1 OR p.channel_id=?1 \
                 ORDER BY r.occurred_at_unix_ms DESC,r.run_id DESC LIMIT ?2",
                )
                .map_err(ScheduleMaterializationHistoryReadError::Storage)?,
        };
        bind_text(self.database, statement.raw, 1, &channel)
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        match cursor {
            Some(cursor) => {
                bind_i64(self.database, statement.raw, 2, cursor.occurred_at_unix_ms)
                    .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
                let run_id = c_string(&cursor.run_id, "materialization history cursor run")
                    .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
                bind_text(self.database, statement.raw, 3, &run_id)
                    .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
                bind_i64(self.database, statement.raw, 4, fetch_limit)
                    .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
            }
            None => bind_i64(self.database, statement.raw, 2, fetch_limit)
                .map_err(ScheduleMaterializationHistoryReadError::Storage)?,
        }

        let mut selected = Vec::with_capacity(limit.saturating_add(1));
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => selected.push(ScheduleMaterializationHistoryCursor {
                    run_id: column_text(statement.raw, 0)
                        .map_err(ScheduleMaterializationHistoryReadError::Integrity)?,
                    occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 1) },
                }),
                SQLITE_DONE => break,
                _ => {
                    return Err(ScheduleMaterializationHistoryReadError::Storage(
                        sqlite_error(self.database),
                    ));
                }
            }
        }
        if selected.len() > limit.saturating_add(1) {
            return Err(ScheduleMaterializationHistoryReadError::Capacity);
        }
        Ok(selected)
    }

    fn read_materialization_history_entry_locked(
        &self,
        channel_id: &str,
        binding: &ScheduleMaterializationHistoryCursor,
    ) -> Result<ScheduleMaterializationHistoryEntry, ScheduleMaterializationHistoryReadError> {
        if binding.occurred_at_unix_ms < 0 || !is_canonical_uuid(&binding.run_id) {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history row is malformed".into(),
            ));
        }
        self.validate_materialization_history_binding_locked(channel_id, binding)?;
        let run = self
            .read_schedule_materialization_run_verified(&binding.run_id)
            .map_err(VerifiedMaterializationRunReadError::into_history_error)?
            .ok_or_else(|| {
                ScheduleMaterializationHistoryReadError::Integrity(
                    "materialization history run disappeared".into(),
                )
            })?;
        if run.occurred_at_unix_ms != binding.occurred_at_unix_ms
            || run.plans.iter().any(|plan| plan.channel_id != channel_id)
            || run
                .outcomes
                .iter()
                .any(|outcome| outcome.channel_id != channel_id)
        {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history run contradicts its channel binding".into(),
            ));
        }
        let generated_drafts = self.read_materialization_history_drafts_locked(channel_id, &run)?;
        let origin = if run.changed_by == AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID {
            ScheduleMaterializationRunOrigin::Automatic
        } else {
            ScheduleMaterializationRunOrigin::Operator
        };
        Ok(ScheduleMaterializationHistoryEntry {
            run,
            origin,
            generated_drafts,
        })
    }

    fn validate_materialization_history_binding_locked(
        &self,
        channel_id: &str,
        binding: &ScheduleMaterializationHistoryCursor,
    ) -> Result<(), ScheduleMaterializationHistoryReadError> {
        let statement = self
            .prepare(
                "SELECT channel_id,occurred_at_unix_ms \
             FROM schedule_materialization_run_channels WHERE run_id=?1",
            )
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let run_id = c_string(&binding.run_id, "materialization history run")
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        bind_text(self.database, statement.raw, 1, &run_id)
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history channel binding disappeared".into(),
            ));
        }
        if status != SQLITE_ROW {
            return Err(ScheduleMaterializationHistoryReadError::Storage(
                sqlite_error(self.database),
            ));
        }
        let bound_channel = column_text(statement.raw, 0)
            .map_err(ScheduleMaterializationHistoryReadError::Integrity)?;
        let bound_occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 1) };
        if bound_channel != channel_id || bound_occurred_at_unix_ms != binding.occurred_at_unix_ms {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history channel binding is contradictory".into(),
            ));
        }
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(()),
            SQLITE_ROW => Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history run has duplicate channel bindings".into(),
            )),
            _ => Err(ScheduleMaterializationHistoryReadError::Storage(
                sqlite_error(self.database),
            )),
        }
    }

    fn read_materialization_history_drafts_locked(
        &self,
        channel_id: &str,
        run: &ScheduleMaterializationRun,
    ) -> Result<Vec<ScheduleMaterializationGeneratedDraft>, ScheduleMaterializationHistoryReadError>
    {
        let expected = run
            .outcomes
            .iter()
            .filter_map(|record| match &record.outcome {
                ScheduleMaterializationOutcome::Proposed {
                    occurrence_id,
                    schedule_item_id,
                    ..
                } => Some((schedule_item_id.as_str(), occurrence_id.as_str())),
                _ => None,
            })
            .collect::<BTreeMap<_, _>>();
        let expected_count = run
            .outcomes
            .iter()
            .filter(|record| {
                matches!(
                    &record.outcome,
                    ScheduleMaterializationOutcome::Proposed { .. }
                )
            })
            .count();
        if expected.len() != expected_count || expected.len() > MAX_BATCH_OUTCOMES {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history run has duplicate or excessive Draft authority".into(),
            ));
        }

        let statement = self
            .prepare(
                "SELECT schedule_item_id FROM schedule_materialization_drafts \
             WHERE run_id=?1 ORDER BY schedule_item_id LIMIT ?2",
            )
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let run_id = c_string(&run.run_id, "materialization history Draft run")
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        bind_text(self.database, statement.raw, 1, &run_id)
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            i64::try_from(MAX_BATCH_OUTCOMES + 1)
                .map_err(|_| ScheduleMaterializationHistoryReadError::Capacity)?,
        )
        .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let mut stored_item_ids = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => stored_item_ids.push(
                    column_text(statement.raw, 0)
                        .map_err(ScheduleMaterializationHistoryReadError::Integrity)?,
                ),
                SQLITE_DONE => break,
                _ => {
                    return Err(ScheduleMaterializationHistoryReadError::Storage(
                        sqlite_error(self.database),
                    ));
                }
            }
        }
        if stored_item_ids.len() > MAX_BATCH_OUTCOMES || stored_item_ids.len() != expected.len() {
            return Err(ScheduleMaterializationHistoryReadError::Integrity(
                "materialization history Draft provenance is incomplete or excessive".into(),
            ));
        }

        let mut generated_drafts = Vec::with_capacity(stored_item_ids.len());
        for schedule_item_id in stored_item_ids {
            let expected_occurrence_id =
                expected.get(schedule_item_id.as_str()).ok_or_else(|| {
                    ScheduleMaterializationHistoryReadError::Integrity(
                        "materialization history Draft provenance has no proposed outcome".into(),
                    )
                })?;
            let origin = self
                .read_materialization_origin_for_verified_run(&schedule_item_id, run)
                .map_err(materialization_write_error_into_history)?
                .ok_or_else(|| {
                    ScheduleMaterializationHistoryReadError::Integrity(
                        "materialization history Draft provenance disappeared".into(),
                    )
                })?;
            if origin.run_id != run.run_id
                || origin.channel_id != channel_id
                || origin.schedule_item_id != schedule_item_id
                || origin.occurrence_id != *expected_occurrence_id
            {
                return Err(ScheduleMaterializationHistoryReadError::Integrity(
                    "materialization history Draft provenance contradicts its run".into(),
                ));
            }
            let current_schedule = self
                .read_schedule_item_for_materialization_history_locked(&schedule_item_id)?
                .ok_or_else(|| {
                    ScheduleMaterializationHistoryReadError::Integrity(
                        "materialization history Draft current schedule disappeared".into(),
                    )
                })?;
            if current_schedule.item_id != schedule_item_id
                || current_schedule.channel_id != channel_id
            {
                return Err(ScheduleMaterializationHistoryReadError::Integrity(
                    "materialization history Draft current schedule linkage is invalid".into(),
                ));
            }
            generated_drafts.push(ScheduleMaterializationGeneratedDraft {
                occurrence_id: origin.occurrence_id,
                schedule_item_id,
                current_schedule,
            });
        }
        Ok(generated_drafts)
    }

    /// Repeats the authoritative current-row/history consistency read with
    /// typed SQLite failures. The public schedule reader intentionally predates
    /// typed read errors and returns a String; history must not collapse an I/O
    /// or prepare/step error into a persisted-integrity response.
    fn read_schedule_item_for_materialization_history_locked(
        &self,
        schedule_item_id: &str,
    ) -> Result<Option<ScheduleItem>, ScheduleMaterializationHistoryReadError> {
        let statement = self
            .prepare(
                "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
                 FROM schedule_items WHERE item_id=?1",
            )
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let item_id = c_string(
            schedule_item_id,
            "materialization history Draft schedule identity",
        )
        .map_err(ScheduleMaterializationHistoryReadError::Integrity)?;
        bind_text(self.database, statement.raw, 1, &item_id)
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        let current = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Some(
                read_schedule_item_row(statement.raw)
                    .map_err(ScheduleMaterializationHistoryReadError::Integrity)?,
            ),
            SQLITE_DONE => None,
            _ => {
                return Err(ScheduleMaterializationHistoryReadError::Storage(
                    sqlite_error(self.database),
                ));
            }
        };
        drop(statement);
        let Some(current) = current else {
            return Ok(None);
        };

        let history = self
            .prepare(
                "SELECT item_id, revision, record_json, changed_by, occurred_at_unix_ms \
                 FROM schedule_item_history WHERE item_id=?1 ORDER BY revision DESC LIMIT 1",
            )
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        bind_text(self.database, history.raw, 1, &item_id)
            .map_err(ScheduleMaterializationHistoryReadError::Storage)?;
        match unsafe { sqlite3_step(history.raw) } {
            SQLITE_ROW => {
                let latest = read_schedule_item_history_row(history.raw)
                    .map_err(ScheduleMaterializationHistoryReadError::Integrity)?;
                if latest.item != current {
                    return Err(ScheduleMaterializationHistoryReadError::Integrity(
                        "materialization history Draft current row and latest history disagree"
                            .into(),
                    ));
                }
            }
            SQLITE_DONE => {}
            _ => {
                return Err(ScheduleMaterializationHistoryReadError::Storage(
                    sqlite_error(self.database),
                ));
            }
        }
        Ok(Some(current))
    }

    fn read_materialization_channel_enabled_locked(
        &self,
        channel_id: &str,
    ) -> Result<Option<bool>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("materialization channel identity is invalid".into());
        }
        let statement = self.prepare("SELECT enabled FROM channels WHERE channel_id=?1")?;
        let channel_id = c_string(channel_id, "materialization channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => match unsafe { sqlite3_column_int64(statement.raw, 0) } {
                0 => Ok(Some(false)),
                1 => Ok(Some(true)),
                _ => Err("materialization channel enabled authority is invalid".into()),
            },
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn latest_materialization_run_binding_locked(
        &self,
        channel_id: &str,
    ) -> Result<Option<(String, i64)>, String> {
        let statement = self.prepare(
            "SELECT run_id,occurred_at_unix_ms FROM schedule_materialization_run_channels WHERE channel_id=?1 ORDER BY occurred_at_unix_ms DESC,run_id DESC LIMIT 1",
        )?;
        let channel_id = c_string(channel_id, "materialization checkpoint channel")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some((column_text(statement.raw, 0)?, unsafe {
                sqlite3_column_int64(statement.raw, 1)
            }))),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn latest_materialization_run_plan_locked(
        &self,
        channel_id: &str,
    ) -> Result<Option<(String, i64)>, String> {
        let statement = self.prepare(
            "SELECT r.run_id,r.occurred_at_unix_ms FROM schedule_materialization_runs r \
             JOIN schedule_materialization_plans p ON p.run_id=r.run_id \
             WHERE p.channel_id=?1 ORDER BY r.occurred_at_unix_ms DESC,r.run_id DESC LIMIT 1",
        )?;
        let channel_id = c_string(channel_id, "materialization plan checkpoint channel")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some((column_text(statement.raw, 0)?, unsafe {
                sqlite3_column_int64(statement.raw, 1)
            }))),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn materialize_schedule_batch_in_transaction(
        &self,
        channel_id: &str,
        explicit_window: Option<&MaterializationWindow>,
        changed_by: &str,
        authority: &impl SourceEvidenceAuthority,
    ) -> Result<ScheduleMaterializationApplyOutcome, ScheduleMaterializationWriteError> {
        if !is_canonical_uuid(channel_id) || !is_canonical_uuid(changed_by) {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization request or attribution is invalid or unbounded".into(),
            ));
        }
        let now_utc_ms =
            station_clock_utc_ms().map_err(ScheduleMaterializationWriteError::Storage)?;
        if let Some(window) = explicit_window
            && window.starts_at_utc_ms < now_utc_ms
        {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization window starts before the transaction-owned station clock".into(),
            ));
        }
        let enabled_rules = self
            .list_recurring_rules(channel_id, 128)
            .map_err(ScheduleMaterializationWriteError::Storage)?
            .into_iter()
            .filter(|rule| rule.enabled)
            .collect::<Vec<_>>();
        if enabled_rules.is_empty() {
            return Err(ScheduleMaterializationWriteError::StaleAuthority(
                "channel has no enabled recurring-rule authority".into(),
            ));
        }
        if enabled_rules.len() > MAX_BATCH_REQUESTS {
            return Err(ScheduleMaterializationWriteError::Capacity);
        }

        let derived_window_start = if explicit_window.is_none() {
            let observed_at_utc_ms = authority
                .observed_at_utc_ms()
                .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
            let start = second_upcoming_utc_minute(observed_at_utc_ms)?;
            if start < now_utc_ms {
                return Err(ScheduleMaterializationWriteError::Invalid(
                    "materialization authority clock is behind the transaction-owned station clock"
                        .into(),
                ));
            }
            Some(start)
        } else {
            None
        };
        let batch_window = match explicit_window {
            Some(window) => window.clone(),
            None => {
                let starts_at_utc_ms = derived_window_start.ok_or_else(|| {
                    ScheduleMaterializationWriteError::Invalid(
                        "derived materialization window start is unavailable".into(),
                    )
                })?;
                let maximum_days = enabled_rules
                    .iter()
                    .map(|rule| rule.rolling_window_days)
                    .max()
                    .ok_or_else(|| {
                        ScheduleMaterializationWriteError::StaleAuthority(
                            "channel has no enabled recurring-rule authority".into(),
                        )
                    })?;
                materialization_window_for_days(starts_at_utc_ms, maximum_days)?
            }
        };

        let candidate_projection = self
            .snapshot_catalog_candidates_in_transaction()
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let mut sources = Vec::with_capacity(enabled_rules.len());
        for rule in enabled_rules {
            let window = match explicit_window {
                Some(window) => window.clone(),
                None => materialization_window_for_days(
                    derived_window_start.ok_or_else(|| {
                        ScheduleMaterializationWriteError::Invalid(
                            "derived materialization window start is unavailable".into(),
                        )
                    })?,
                    rule.rolling_window_days,
                )?,
            };
            let search = self
                .read_saved_search_revision(&rule.saved_search_id, rule.saved_search_revision)
                .map_err(ScheduleMaterializationWriteError::Storage)?
                .ok_or_else(|| {
                    ScheduleMaterializationWriteError::StaleAuthority(format!(
                        "saved-search authority for rule {} is unavailable",
                        rule.rule_id
                    ))
                })?;
            let block = self
                .read_daypart_block_revision(&rule.block_id, rule.block_revision)
                .map_err(ScheduleMaterializationWriteError::Storage)?
                .ok_or_else(|| {
                    ScheduleMaterializationWriteError::StaleAuthority(format!(
                        "daypart authority for rule {} is unavailable",
                        rule.rule_id
                    ))
                })?;
            rule.validate_against(&search, &block)
                .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
            let prior_airings_coverage =
                materialization_prior_airings_coverage_window(&rule, &window)
                    .map_err(ScheduleMaterializationWriteError::Invalid)?;
            let raw_airings = self
                .list_actual_airings_for_materialization(
                    &rule.channel_id,
                    prior_airings_coverage.starts_at_utc_ms,
                    prior_airings_coverage.ends_at_utc_ms,
                )
                .map_err(|error| {
                    ScheduleMaterializationWriteError::Storage(format!(
                        "read materialization prior-airing authority: {error:?}"
                    ))
                })?;
            let prior_airings = raw_airings
                .into_iter()
                .map(|fact| (fact.asset_sha256, fact.aired_at_utc_ms))
                .collect::<BTreeSet<_>>()
                .into_iter()
                .map(|(asset_sha256, aired_at_utc_ms)| PriorAiring {
                    asset_sha256,
                    aired_at_utc_ms,
                })
                .collect::<Vec<_>>();
            if prior_airings.len() > MAX_PRIOR_AIRINGS {
                return Err(ScheduleMaterializationWriteError::Capacity);
            }
            let prior_airings_sha256 = canonical_prior_airings_sha256(&prior_airings)
                .map_err(ScheduleMaterializationWriteError::Invalid)?;
            sources.push(ScheduleMaterializationSourceSnapshot {
                search,
                block,
                rule,
                candidate_projection: candidate_projection.clone(),
                prior_airings,
                prior_airings_sha256,
                prior_airings_coverage,
                window,
            });
        }
        let mut authenticated_inputs = Vec::with_capacity(sources.len());
        for source in &sources {
            let candidate_scope = materialization_evidence_scope_sha256(
                &source.search,
                &source.block,
                &source.rule,
                &source.window,
                SourceEvidenceKind::CandidateProjection,
            )
            .map_err(ScheduleMaterializationWriteError::Invalid)?;
            let prior_scope = materialization_evidence_scope_sha256(
                &source.search,
                &source.block,
                &source.rule,
                &source.window,
                SourceEvidenceKind::PriorAirings,
            )
            .map_err(ScheduleMaterializationWriteError::Invalid)?;
            let candidate_receipt = authority
                .issue_source_evidence(
                    SourceEvidenceKind::CandidateProjection,
                    &candidate_scope,
                    &source.candidate_projection.candidate_projection_sha256,
                    MATERIALIZATION_RECEIPT_LIFETIME_MS,
                )
                .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
            let prior_receipt = authority
                .issue_source_evidence(
                    SourceEvidenceKind::PriorAirings,
                    &prior_scope,
                    &source.prior_airings_sha256,
                    MATERIALIZATION_RECEIPT_LIFETIME_MS,
                )
                .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
            authenticated_inputs.push(AuthenticatedMaterializationInputs {
                candidate_projection: AuthenticatedCandidateProjection {
                    receipt: candidate_receipt,
                    candidates: source.candidate_projection.candidates.clone(),
                },
                prior_airings: AuthenticatedPriorAirings {
                    receipt: prior_receipt,
                    airings: source.prior_airings.clone(),
                },
            });
        }
        let observed_at_utc_ms = authority
            .observed_at_utc_ms()
            .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
        if observed_at_utc_ms < 0
            || sources
                .iter()
                .any(|source| source.window.starts_at_utc_ms < observed_at_utc_ms)
        {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization authority time is invalid or already past the write window".into(),
            ));
        }
        let compile_requests = sources
            .iter()
            .zip(&authenticated_inputs)
            .map(|(source, inputs)| WindowedBatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &source.search,
                    block: &source.block,
                    rule: &source.rule,
                },
                inputs,
                window: source.window.clone(),
            })
            .collect::<Vec<_>>();
        let batch = derive_arbitrated_materialization_batch_for_windows(
            &compile_requests,
            observed_at_utc_ms,
            authority,
        )
        .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;

        if batch.plans().len() != sources.len() {
            return Err(ScheduleMaterializationWriteError::StaleAuthority(
                "compiler result does not cover the complete enabled channel rule set".into(),
            ));
        }
        let authority_by_rule = sources
            .iter()
            .map(|source| {
                (
                    source.rule.rule_id.as_str(),
                    (&source.search, &source.block, &source.rule, source),
                )
            })
            .collect::<BTreeMap<_, _>>();
        let mut covered_rules = BTreeSet::new();
        for plan in batch.plans() {
            let binding = plan.authority_binding();
            let (search, block, rule, source) = authority_by_rule
                .get(binding.rule_id.as_str())
                .ok_or_else(|| {
                    ScheduleMaterializationWriteError::StaleAuthority(
                        "compiled plan is not bound to a requested rule".into(),
                    )
                })?;
            if !covered_rules.insert(binding.rule_id.as_str())
                || binding.window != source.window
                || plan.source_input_binding.candidate_projection_sha256
                    != source.candidate_projection.candidate_projection_sha256
                || plan.source_input_binding.prior_airings_sha256 != source.prior_airings_sha256
                || plan.source_input_binding.prior_airings_coverage != source.prior_airings_coverage
            {
                return Err(ScheduleMaterializationWriteError::StaleAuthority(
                    "compiler result is incomplete or source-rebound".into(),
                ));
            }
            plan.validate_authority_against(search, block, rule)
                .map_err(ScheduleMaterializationWriteError::StaleAuthority)?;
        }

        let mut records = materialization_records(&batch)?;
        if records.len() > MAX_BATCH_OUTCOMES {
            return Err(ScheduleMaterializationWriteError::Capacity);
        }
        let plans = plan_records(&batch)?;
        let plan_digests = plan_digests(&batch);
        let semantic_outcomes = records
            .iter()
            .map(|record| SemanticOutcomeDigest {
                ordinal: record.ordinal,
                rule_id: record.rule_id.clone(),
                rule_revision: record.rule_revision,
                channel_id: record.channel_id.clone(),
                block_id: record.block_id.clone(),
                block_revision: record.block_revision,
                source_authority_sha256: record.source_authority_sha256.clone(),
                outcome: record.compiled_outcome.clone(),
            })
            .collect::<Vec<_>>();
        let digest_json = serde_json::to_vec(&BatchDigest {
            schema: "townlight.schedule-materialization-run.v1",
            window_starts_at_utc_ms: batch_window.starts_at_utc_ms,
            window_ends_at_utc_ms: batch_window.ends_at_utc_ms,
            plans: &plan_digests,
            outcomes: &semantic_outcomes,
        })
        .map_err(|error| {
            ScheduleMaterializationWriteError::Storage(format!(
                "serialize materialization digest: {error}"
            ))
        })?;
        let request_sha256 = lowercase_sha256(&digest_json);
        self.reconcile_materialization_capacity_and_collisions(&request_sha256, &mut records)?;
        let result_outcomes = result_outcome_digests(&records);
        let result_json = serde_json::to_vec(&ResultDigest {
            schema: "townlight.schedule-materialization-result.v1",
            request_sha256: &request_sha256,
            outcomes: &result_outcomes,
        })
        .map_err(|error| {
            ScheduleMaterializationWriteError::Storage(format!(
                "serialize materialization result digest: {error}"
            ))
        })?;
        let result_sha256 = lowercase_sha256(&result_json);
        let run_id =
            deterministic_uuid_v8("townlight.schedule-materialization-run.v1", &result_sha256);
        if let Some(existing) = self
            .read_schedule_materialization_run(&run_id)
            .map_err(ScheduleMaterializationWriteError::Storage)?
        {
            if existing.request_sha256 != request_sha256 || existing.result_sha256 != result_sha256
            {
                return Err(ScheduleMaterializationWriteError::Collision(
                    "materialization run identity collided with different durable content".into(),
                ));
            }
            self.reconcile_schedule_no_match_alert_locked(
                &existing.run_id,
                channel_id,
                observed_at_utc_ms,
                &existing.outcomes,
                false,
            )?;
            return Ok(ScheduleMaterializationApplyOutcome::AlreadyApplied(
                existing,
            ));
        }

        self.insert_materialization_run(
            &run_id,
            channel_id,
            &request_sha256,
            &result_sha256,
            &batch_window,
            observed_at_utc_ms,
            changed_by,
            observed_at_utc_ms,
            records.len(),
            plans.len(),
        )?;
        for plan in &plans {
            self.insert_materialization_plan(&run_id, plan)?;
        }
        for record in &records {
            if let ScheduleMaterializationOutcome::Proposed {
                occurrence_id: _,
                schedule_item_id,
                asset_sha256,
                asset_title,
                starts_at_utc_ms,
                ends_at_utc_ms,
            } = &record.outcome
            {
                let duration_ms =
                    u64::try_from(ends_at_utc_ms - starts_at_utc_ms).map_err(|_| {
                        ScheduleMaterializationWriteError::Invalid(
                            "materialized schedule duration is invalid".into(),
                        )
                    })?;
                let item = ScheduleItem {
                    item_id: schedule_item_id.clone(),
                    revision: 1,
                    channel_id: record.channel_id.clone(),
                    asset_id: asset_sha256.clone(),
                    title: asset_title.clone(),
                    starts_at_unix_ms: *starts_at_utc_ms,
                    duration_ms,
                    state: ScheduleState::Draft,
                };
                item.validate().map_err(|error| {
                    ScheduleMaterializationWriteError::Invalid(format!(
                        "materialized Draft is invalid: {error:?}"
                    ))
                })?;
                self.insert_schedule_item(&item)
                    .map_err(ScheduleMaterializationWriteError::Storage)?;
                self.append_schedule_item_history(&item, changed_by, observed_at_utc_ms)
                    .map_err(ScheduleMaterializationWriteError::Storage)?;
                self.insert_materialization_draft(&run_id, record)?;
            }
            self.insert_materialization_outcome(&run_id, record)?;
        }
        self.reconcile_schedule_no_match_alert_locked(
            &run_id,
            channel_id,
            observed_at_utc_ms,
            &records,
            true,
        )?;

        Ok(ScheduleMaterializationApplyOutcome::Applied(
            ScheduleMaterializationRun {
                run_id,
                request_sha256,
                result_sha256: result_sha256.clone(),
                batch_sha256: result_sha256,
                window: batch_window,
                observed_at_utc_ms,
                changed_by: changed_by.to_string(),
                occurred_at_unix_ms: observed_at_utc_ms,
                plans,
                outcomes: records,
            },
        ))
    }

    fn reconcile_schedule_no_match_alert_locked(
        &self,
        run_id: &str,
        channel_id: &str,
        observed_at_utc_ms: i64,
        records: &[ScheduleMaterializationOutcomeRecord],
        is_new_run: bool,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let no_matches = records
            .iter()
            .filter_map(|record| match &record.outcome {
                ScheduleMaterializationOutcome::NoMatch { starts_at_utc_ms } => {
                    Some((record.rule_id.as_str(), *starts_at_utc_ms))
                }
                _ => None,
            })
            .collect::<Vec<_>>();
        if no_matches.is_empty() {
            self.resolve_alert_locked(
                AlertConditionKind::ScheduleNoMatch,
                channel_id,
                observed_at_utc_ms,
            )
            .map_err(ScheduleMaterializationWriteError::Storage)?;
            return Ok(());
        }
        if !is_new_run
            && self
                .read_firing_alert(AlertConditionKind::ScheduleNoMatch, channel_id)
                .map_err(ScheduleMaterializationWriteError::Storage)?
                .is_some()
        {
            return Ok(());
        }

        let earliest = no_matches
            .iter()
            .map(|(_, starts_at_utc_ms)| *starts_at_utc_ms)
            .min()
            .ok_or_else(|| {
                ScheduleMaterializationWriteError::Storage(
                    "no-match alert aggregation produced no earliest slot".into(),
                )
            })?;
        let rules = no_matches
            .iter()
            .map(|(rule_id, _)| *rule_id)
            .collect::<BTreeSet<_>>();
        let displayed_rules = rules.iter().take(8).copied().collect::<Vec<_>>().join(", ");
        let omitted_rules = rules.len().saturating_sub(8);
        let rule_detail = if omitted_rules == 0 {
            displayed_rules
        } else {
            format!("{displayed_rules} (+{omitted_rules} more)")
        };
        let episode = self
            .next_alert_episode_ordinal_locked(AlertConditionKind::ScheduleNoMatch, channel_id)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        self.observe_alert_and_route_locked(&AlertEvent {
            event_id: format!("schedule-no-match-{run_id}-{episode}"),
            condition: AlertConditionKind::ScheduleNoMatch,
            severity: AlertSeverity::Warning,
            state: AlertEventState::Firing,
            resource_ref: channel_id.into(),
            summary: format!(
                "Recurring schedule generation found {} slots with no matching asset.",
                no_matches.len()
            ),
            detail: format!(
                "Materialization run {run_id} found no published, ready catalog asset that satisfied the saved-search and repeat policy for {} recurring slots. Earliest affected UTC millisecond: {earliest}. Affected recurring rule identities: {rule_detail}. Review S19 saved-search predicates, catalog editorial/readiness state, publication time, and repeat limits.",
                no_matches.len()
            ),
            source_section: "S19".into(),
            first_observed_at_unix_ms: observed_at_utc_ms,
            last_observed_at_unix_ms: observed_at_utc_ms,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        })
        .map_err(ScheduleMaterializationWriteError::Storage)?;
        Ok(())
    }

    fn reconcile_materialization_capacity_and_collisions(
        &self,
        request_sha256: &str,
        records: &mut [ScheduleMaterializationOutcomeRecord],
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let mut proposed_per_channel = BTreeMap::<&str, usize>::new();
        for record in records {
            let ScheduleMaterializationOutcome::Proposed {
                schedule_item_id,
                occurrence_id,
                asset_sha256,
                asset_title,
                starts_at_utc_ms,
                ends_at_utc_ms,
            } = record.outcome.clone()
            else {
                continue;
            };
            let origin = self.read_materialization_origin(&schedule_item_id)?;
            if let Some(existing) = self
                .read_schedule_item(&schedule_item_id)
                .map_err(ScheduleMaterializationWriteError::Storage)?
            {
                let origin = origin.ok_or_else(|| {
                    ScheduleMaterializationWriteError::Collision(format!(
                        "deterministic schedule identity {schedule_item_id} belongs to a manual row"
                    ))
                })?;
                if origin.occurrence_id != occurrence_id
                    || origin.schedule_item_id != schedule_item_id
                {
                    return Err(ScheduleMaterializationWriteError::Collision(
                        "deterministic schedule identity has contradictory occurrence provenance"
                            .into(),
                    ));
                }
                if self.materialized_schedule_item_has_aired(&schedule_item_id)? {
                    record.outcome = ScheduleMaterializationOutcome::AlreadyAired {
                        occurrence_id,
                        schedule_item_id,
                        existing: existing_schedule_snapshot(&existing),
                    };
                    continue;
                }
                let existing_snapshot = existing_schedule_snapshot(&existing);
                let creator_matches_request = self
                    .read_schedule_materialization_run(&origin.run_id)
                    .map_err(ScheduleMaterializationWriteError::Storage)?
                    .is_some_and(|run| run.request_sha256 == request_sha256);
                record.outcome = match existing.state {
                    ScheduleState::Committed => ScheduleMaterializationOutcome::ExistingCommitted {
                        occurrence_id,
                        schedule_item_id,
                        existing: existing_snapshot,
                    },
                    ScheduleState::Cancelled => {
                        ScheduleMaterializationOutcome::CancelledOccurrence {
                            occurrence_id,
                            schedule_item_id,
                            existing: existing_snapshot,
                        }
                    }
                    ScheduleState::Draft
                        if existing.asset_id == asset_sha256
                            && existing.title == asset_title
                            && existing.starts_at_unix_ms == starts_at_utc_ms
                            && existing.ends_at_unix_ms().ok() == Some(ends_at_utc_ms)
                            && creator_matches_request
                            && origin.original_schedule == existing_snapshot =>
                    {
                        record.compiled_outcome.clone()
                    }
                    ScheduleState::Draft
                        if existing.asset_id == asset_sha256
                            && existing.title == asset_title
                            && existing.starts_at_unix_ms == starts_at_utc_ms
                            && existing.ends_at_unix_ms().ok() == Some(ends_at_utc_ms) =>
                    {
                        ScheduleMaterializationOutcome::ExistingAutomationDraftUnchanged {
                            occurrence_id,
                            schedule_item_id,
                            existing: existing_snapshot,
                        }
                    }
                    ScheduleState::Draft => {
                        ScheduleMaterializationOutcome::ExistingAutomationDraftChanged {
                            occurrence_id,
                            schedule_item_id,
                            existing: existing_snapshot,
                        }
                    }
                };
                continue;
            }
            if origin.is_some() {
                return Err(ScheduleMaterializationWriteError::Collision(
                    "materialization provenance points to a missing schedule row".into(),
                ));
            }
            if let Some((existing, owner)) = self.highest_precedence_schedule_overlap(
                &record.channel_id,
                starts_at_utc_ms,
                ends_at_utc_ms,
            )? {
                record.outcome = ScheduleMaterializationOutcome::SuppressedByExistingSchedule {
                    occurrence_id,
                    schedule_item_id,
                    existing: existing_schedule_snapshot(&existing),
                    existing_owner: owner.into(),
                };
                continue;
            }
            *proposed_per_channel
                .entry(record.channel_id.as_str())
                .or_default() += 1;
        }
        for (channel_id, proposed) in proposed_per_channel {
            let count = self.prepare(
                "SELECT COUNT(*) FROM schedule_materialization_drafts d JOIN schedule_items s ON s.item_id=d.schedule_item_id WHERE d.channel_id=?1 AND s.state='draft'",
            ).map_err(ScheduleMaterializationWriteError::Storage)?;
            let channel = c_string(channel_id, "materialization capacity channel")
                .map_err(ScheduleMaterializationWriteError::Storage)?;
            bind_text(self.database, count.raw, 1, &channel)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
            if unsafe { sqlite3_step(count.raw) } != SQLITE_ROW {
                return Err(ScheduleMaterializationWriteError::Storage(sqlite_error(
                    self.database,
                )));
            }
            let current = usize::try_from(unsafe { sqlite3_column_int64(count.raw, 0) })
                .map_err(|_| ScheduleMaterializationWriteError::Capacity)?;
            if current.saturating_add(proposed) > MAX_AUTOMATION_DRAFTS_PER_CHANNEL {
                return Err(ScheduleMaterializationWriteError::Capacity);
            }
        }
        Ok(())
    }

    fn materialized_schedule_item_has_aired(
        &self,
        schedule_item_id: &str,
    ) -> Result<bool, ScheduleMaterializationWriteError> {
        let statement = self
            .prepare("SELECT 1 FROM actual_airings WHERE schedule_item_id=?1 LIMIT 1")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let id = c_string(schedule_item_id, "materialization airing schedule identity")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_text(self.database, statement.raw, 1, &id)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(ScheduleMaterializationWriteError::Storage(sqlite_error(
                self.database,
            ))),
        }
    }

    fn highest_precedence_schedule_overlap(
        &self,
        channel_id: &str,
        starts_at_utc_ms: i64,
        ends_at_utc_ms: i64,
    ) -> Result<Option<(ScheduleItem, &'static str)>, ScheduleMaterializationWriteError> {
        let statement = self.prepare(
            "SELECT item_id FROM schedule_items WHERE channel_id=?1 AND state IN ('draft','committed') \
             AND starts_at_unix_ms < ?2 AND duration_ms > (?3 - starts_at_unix_ms) \
             ORDER BY starts_at_unix_ms,item_id LIMIT 129",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        let channel = c_string(channel_id, "materialization overlap channel")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_text(self.database, statement.raw, 1, &channel)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_i64(self.database, statement.raw, 2, ends_at_utc_ms)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_i64(self.database, statement.raw, 3, starts_at_utc_ms)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let mut candidates = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => candidates.push(
                    column_text(statement.raw, 0)
                        .map_err(ScheduleMaterializationWriteError::Storage)?,
                ),
                SQLITE_DONE => break,
                _ => {
                    return Err(ScheduleMaterializationWriteError::Storage(sqlite_error(
                        self.database,
                    )));
                }
            }
        }
        if candidates.len() > 128 {
            return Err(ScheduleMaterializationWriteError::Capacity);
        }
        let mut winner: Option<(u8, ScheduleItem, &'static str)> = None;
        for item_id in candidates {
            let item = self
                .read_schedule_item(&item_id)
                .map_err(ScheduleMaterializationWriteError::Storage)?
                .ok_or_else(|| {
                    ScheduleMaterializationWriteError::Storage(
                        "overlap query returned a missing schedule item".into(),
                    )
                })?;
            let item_end = item.ends_at_unix_ms().map_err(|error| {
                ScheduleMaterializationWriteError::Storage(format!(
                    "stored overlap has invalid duration: {error:?}"
                ))
            })?;
            if item.starts_at_unix_ms >= ends_at_utc_ms || item_end <= starts_at_utc_ms {
                continue;
            }
            let automation_owned = self.read_materialization_origin(&item.item_id)?.is_some();
            let (rank, owner) = match item.state {
                ScheduleState::Committed => (3, "committed"),
                ScheduleState::Draft if automation_owned => (1, "automation"),
                ScheduleState::Draft => (2, "manual"),
                ScheduleState::Cancelled => continue,
            };
            if winner
                .as_ref()
                .is_none_or(|(current, _, _)| rank > *current)
            {
                winner = Some((rank, item, owner));
            }
        }
        Ok(winner.map(|(_, item, owner)| (item, owner)))
    }

    #[allow(clippy::too_many_arguments)]
    fn insert_materialization_run(
        &self,
        run_id: &str,
        channel_id: &str,
        request_sha256: &str,
        result_sha256: &str,
        window: &MaterializationWindow,
        observed_at_utc_ms: i64,
        changed_by: &str,
        occurred_at_unix_ms: i64,
        outcome_count: usize,
        plan_count: usize,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        if !is_canonical_uuid(changed_by) {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "materialization attribution must be a canonical UUID".into(),
            ));
        }
        let statement = self.prepare(
            "INSERT INTO schedule_materialization_runs(run_id,request_sha256,result_sha256,window_starts_at_utc_ms,window_ends_at_utc_ms,observed_at_utc_ms,changed_by,occurred_at_unix_ms,plan_count,outcome_count) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        for (index, (value, name)) in [
            (run_id, "materialization run identity"),
            (request_sha256, "materialization request digest"),
            (result_sha256, "materialization result digest"),
            (changed_by, "materialization actor"),
        ]
        .into_iter()
        .enumerate()
        {
            let value =
                c_string(value, name).map_err(ScheduleMaterializationWriteError::Storage)?;
            let parameter = match index {
                0 => 1,
                1 => 2,
                2 => 3,
                _ => 7,
            };
            bind_text(self.database, statement.raw, parameter, &value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        for (index, value) in [
            window.starts_at_utc_ms,
            window.ends_at_utc_ms,
            observed_at_utc_ms,
            occurred_at_unix_ms,
            i64::try_from(plan_count).map_err(|_| ScheduleMaterializationWriteError::Capacity)?,
            i64::try_from(outcome_count)
                .map_err(|_| ScheduleMaterializationWriteError::Capacity)?,
        ]
        .into_iter()
        .enumerate()
        {
            let parameter = match index {
                0 => 4,
                1 => 5,
                2 => 6,
                3 => 8,
                4 => 9,
                _ => 10,
            };
            bind_i64(self.database, statement.raw, parameter, value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        expect_done(self.database, statement.raw)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        drop(statement);
        let binding = self
            .prepare(
                "INSERT INTO schedule_materialization_run_channels(run_id,channel_id,occurred_at_unix_ms) VALUES(?1,?2,?3)",
            )
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let run_id = c_string(run_id, "materialization run channel binding")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        let channel_id = c_string(channel_id, "materialization run bound channel")
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_text(self.database, binding.raw, 1, &run_id)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_text(self.database, binding.raw, 2, &channel_id)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        bind_i64(self.database, binding.raw, 3, occurred_at_unix_ms)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        expect_done(self.database, binding.raw).map_err(ScheduleMaterializationWriteError::Storage)
    }

    fn insert_materialization_outcome(
        &self,
        run_id: &str,
        record: &ScheduleMaterializationOutcomeRecord,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let record_json = serde_json::to_string(record).map_err(|error| {
            ScheduleMaterializationWriteError::Storage(format!(
                "serialize materialization outcome: {error}"
            ))
        })?;
        let statement = self.prepare(
            "INSERT INTO schedule_materialization_outcomes(run_id,ordinal,rule_id,channel_id,record_json) VALUES(?1,?2,?3,?4,?5)",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        for (index, (value, name)) in [
            (run_id, "materialization outcome run"),
            (&record.rule_id, "materialization outcome rule"),
            (&record.channel_id, "materialization outcome channel"),
            (&record_json, "materialization outcome record"),
        ]
        .into_iter()
        .enumerate()
        {
            let value =
                c_string(value, name).map_err(ScheduleMaterializationWriteError::Storage)?;
            let parameter = match index {
                0 => 1,
                1 => 3,
                2 => 4,
                _ => 5,
            };
            bind_text(self.database, statement.raw, parameter, &value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        bind_i64(self.database, statement.raw, 2, i64::from(record.ordinal))
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        expect_done(self.database, statement.raw)
            .map_err(ScheduleMaterializationWriteError::Storage)
    }

    fn insert_materialization_plan(
        &self,
        run_id: &str,
        record: &ScheduleMaterializationPlanRecord,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let record_json = serde_json::to_string(record).map_err(|error| {
            ScheduleMaterializationWriteError::Storage(format!(
                "serialize materialization plan: {error}"
            ))
        })?;
        let statement = self.prepare(
            "INSERT INTO schedule_materialization_plans(run_id,ordinal,rule_id,channel_id,record_json) VALUES(?1,?2,?3,?4,?5)",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        for (parameter, value, name) in [
            (1, run_id, "materialization plan run"),
            (3, record.rule_id.as_str(), "materialization plan rule"),
            (
                4,
                record.channel_id.as_str(),
                "materialization plan channel",
            ),
            (5, record_json.as_str(), "materialization plan record"),
        ] {
            let value =
                c_string(value, name).map_err(ScheduleMaterializationWriteError::Storage)?;
            bind_text(self.database, statement.raw, parameter, &value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        bind_i64(self.database, statement.raw, 2, i64::from(record.ordinal))
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        expect_done(self.database, statement.raw)
            .map_err(ScheduleMaterializationWriteError::Storage)
    }

    fn insert_materialization_draft(
        &self,
        run_id: &str,
        record: &ScheduleMaterializationOutcomeRecord,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let ScheduleMaterializationOutcome::Proposed {
            occurrence_id,
            schedule_item_id,
            asset_sha256,
            asset_title,
            starts_at_utc_ms,
            ends_at_utc_ms,
        } = &record.outcome
        else {
            return Err(ScheduleMaterializationWriteError::Invalid(
                "only a proposed outcome may own Draft provenance".into(),
            ));
        };
        let materialization_id = record.materialization_id.as_deref().ok_or_else(|| {
            ScheduleMaterializationWriteError::Invalid(
                "proposed outcome has no materialization identity".into(),
            )
        })?;
        let origin = ScheduleMaterializationOrigin {
            occurrence_id: occurrence_id.clone(),
            schedule_item_id: schedule_item_id.clone(),
            channel_id: record.channel_id.clone(),
            saved_search_id: record.saved_search_id.clone(),
            saved_search_revision: record.saved_search_revision,
            rule_id: record.rule_id.clone(),
            rule_revision: record.rule_revision,
            block_id: record.block_id.clone(),
            block_revision: record.block_revision,
            plan_window: record.plan_window.clone(),
            materialization_id: materialization_id.into(),
            source_authority_sha256: record.source_authority_sha256.clone(),
            source_input_sha256: record.source_input_sha256.clone(),
            candidate_projection_sha256: record.candidate_projection_sha256.clone(),
            prior_airings_sha256: record.prior_airings_sha256.clone(),
            prior_airings_coverage: record.prior_airings_coverage.clone(),
            scope_sha256: record.scope_sha256.clone(),
            original_schedule: ExistingScheduleSnapshot {
                schedule_item_id: schedule_item_id.clone(),
                revision: 1,
                channel_id: record.channel_id.clone(),
                asset_sha256: asset_sha256.clone(),
                title: asset_title.clone(),
                starts_at_utc_ms: *starts_at_utc_ms,
                duration_ms: u64::try_from(ends_at_utc_ms - starts_at_utc_ms).map_err(|_| {
                    ScheduleMaterializationWriteError::Invalid(
                        "materialization origin duration is invalid".into(),
                    )
                })?,
                state: "draft".into(),
            },
            run_id: run_id.into(),
        };
        let record_json = serde_json::to_string(&origin).map_err(|error| {
            ScheduleMaterializationWriteError::Storage(format!(
                "serialize materialization origin: {error}"
            ))
        })?;
        let statement = self.prepare(
            "INSERT INTO schedule_materialization_drafts(schedule_item_id,run_id,occurrence_id,channel_id,saved_search_id,saved_search_revision,rule_id,rule_revision,block_id,block_revision,plan_starts_at_utc_ms,plan_ends_at_utc_ms,materialization_id,source_authority_sha256,source_input_sha256,candidate_projection_sha256,prior_airings_sha256,prior_coverage_starts_at_utc_ms,prior_coverage_ends_at_utc_ms,scope_sha256,original_revision,original_asset_sha256,original_title,original_starts_at_utc_ms,original_duration_ms,original_state,record_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26,?27)",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        for (parameter, value, name) in [
            (
                1,
                schedule_item_id.as_str(),
                "materialization Draft identity",
            ),
            (2, run_id, "materialization Draft run"),
            (
                3,
                occurrence_id.as_str(),
                "materialization occurrence identity",
            ),
            (
                4,
                record.channel_id.as_str(),
                "materialization Draft channel",
            ),
            (
                5,
                record.saved_search_id.as_str(),
                "materialization saved search",
            ),
            (7, record.rule_id.as_str(), "materialization Draft rule"),
            (9, record.block_id.as_str(), "materialization Draft block"),
            (13, materialization_id, "materialization identity"),
            (
                14,
                record.source_authority_sha256.as_str(),
                "materialization source authority",
            ),
            (
                15,
                record.source_input_sha256.as_str(),
                "materialization source input",
            ),
            (
                16,
                record.candidate_projection_sha256.as_str(),
                "materialization candidate projection",
            ),
            (
                17,
                record.prior_airings_sha256.as_str(),
                "materialization prior airings",
            ),
            (20, record.scope_sha256.as_str(), "materialization scope"),
            (22, asset_sha256.as_str(), "materialization original asset"),
            (23, asset_title.as_str(), "materialization original title"),
            (26, "draft", "materialization original state"),
            (27, record_json.as_str(), "materialization origin record"),
        ] {
            let value =
                c_string(value, name).map_err(ScheduleMaterializationWriteError::Storage)?;
            bind_text(self.database, statement.raw, parameter, &value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        for (parameter, revision) in [
            (6, record.saved_search_revision),
            (8, record.rule_revision),
            (10, record.block_revision),
            (21, 1),
        ] {
            bind_i64(
                self.database,
                statement.raw,
                parameter,
                checked_i64(revision, "materialization origin revision")
                    .map_err(ScheduleMaterializationWriteError::Storage)?,
            )
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        for (parameter, value) in [
            (11, record.plan_window.starts_at_utc_ms),
            (12, record.plan_window.ends_at_utc_ms),
            (18, record.prior_airings_coverage.starts_at_utc_ms),
            (19, record.prior_airings_coverage.ends_at_utc_ms),
            (24, *starts_at_utc_ms),
            (
                25,
                i64::try_from(origin.original_schedule.duration_ms).map_err(|_| {
                    ScheduleMaterializationWriteError::Invalid(
                        "materialization origin duration is unbounded".into(),
                    )
                })?,
            ),
        ] {
            bind_i64(self.database, statement.raw, parameter, value)
                .map_err(ScheduleMaterializationWriteError::Storage)?;
        }
        expect_done(self.database, statement.raw)
            .map_err(ScheduleMaterializationWriteError::Storage)
    }

    fn read_materialization_origin(
        &self,
        schedule_item_id: &str,
    ) -> Result<Option<ScheduleMaterializationOrigin>, ScheduleMaterializationWriteError> {
        let origin = self.read_materialization_origin_indexed(schedule_item_id)?;
        let Some(origin) = origin else {
            return Ok(None);
        };
        let creator = self
            .read_schedule_materialization_run(&origin.run_id)
            .map_err(ScheduleMaterializationWriteError::Storage)?
            .ok_or_else(|| {
                ScheduleMaterializationWriteError::Collision(
                    "materialization origin creator run is missing".into(),
                )
            })?;
        Self::validate_materialization_origin_creator(&origin, &creator)?;
        Ok(Some(origin))
    }

    fn read_materialization_origin_for_verified_run(
        &self,
        schedule_item_id: &str,
        creator: &ScheduleMaterializationRun,
    ) -> Result<Option<ScheduleMaterializationOrigin>, ScheduleMaterializationWriteError> {
        let origin = self.read_materialization_origin_indexed(schedule_item_id)?;
        let Some(origin) = origin else {
            return Ok(None);
        };
        if origin.run_id != creator.run_id {
            return Err(ScheduleMaterializationWriteError::Collision(
                "materialization origin belongs to a different creator run".into(),
            ));
        }
        Self::validate_materialization_origin_creator(&origin, creator)?;
        Ok(Some(origin))
    }

    fn validate_materialization_origin_creator(
        origin: &ScheduleMaterializationOrigin,
        creator: &ScheduleMaterializationRun,
    ) -> Result<(), ScheduleMaterializationWriteError> {
        let creator_matches = creator.outcomes.iter().any(|record| {
            record.rule_id == origin.rule_id
                && record.rule_revision == origin.rule_revision
                && record.channel_id == origin.channel_id
                && record.saved_search_id == origin.saved_search_id
                && record.saved_search_revision == origin.saved_search_revision
                && record.block_id == origin.block_id
                && record.block_revision == origin.block_revision
                && record.plan_window == origin.plan_window
                && record.materialization_id.as_deref() == Some(origin.materialization_id.as_str())
                && record.source_authority_sha256 == origin.source_authority_sha256
                && record.source_input_sha256 == origin.source_input_sha256
                && record.candidate_projection_sha256 == origin.candidate_projection_sha256
                && record.prior_airings_sha256 == origin.prior_airings_sha256
                && record.prior_airings_coverage == origin.prior_airings_coverage
                && record.scope_sha256 == origin.scope_sha256
                && origin.original_schedule.revision == 1
                && origin.original_schedule.state == "draft"
                && matches!(
                    &record.outcome,
                    ScheduleMaterializationOutcome::Proposed {
                        occurrence_id,
                        schedule_item_id,
                        asset_sha256,
                        asset_title,
                        starts_at_utc_ms,
                        ends_at_utc_ms,
                    } if occurrence_id == &origin.occurrence_id
                        && schedule_item_id == &origin.schedule_item_id
                        && asset_sha256 == &origin.original_schedule.asset_sha256
                        && asset_title == &origin.original_schedule.title
                        && *starts_at_utc_ms == origin.original_schedule.starts_at_utc_ms
                        && ends_at_utc_ms.checked_sub(*starts_at_utc_ms)
                            == i64::try_from(origin.original_schedule.duration_ms).ok()
                )
        });
        if !creator_matches {
            return Err(ScheduleMaterializationWriteError::Collision(
                "materialization origin does not match its creator outcome".into(),
            ));
        }
        Ok(())
    }

    fn read_materialization_origin_indexed(
        &self,
        schedule_item_id: &str,
    ) -> Result<Option<ScheduleMaterializationOrigin>, ScheduleMaterializationWriteError> {
        let statement = self.prepare(
            "SELECT occurrence_id,run_id,channel_id,saved_search_id,saved_search_revision,rule_id,rule_revision,block_id,block_revision,plan_starts_at_utc_ms,plan_ends_at_utc_ms,materialization_id,source_authority_sha256,source_input_sha256,candidate_projection_sha256,prior_airings_sha256,prior_coverage_starts_at_utc_ms,prior_coverage_ends_at_utc_ms,scope_sha256,original_revision,original_asset_sha256,original_title,original_starts_at_utc_ms,original_duration_ms,original_state,record_json FROM schedule_materialization_drafts WHERE schedule_item_id=?1",
        ).map_err(ScheduleMaterializationWriteError::Storage)?;
        let id = c_string(schedule_item_id, "materialization origin schedule identity")
            .map_err(ScheduleMaterializationWriteError::Invalid)?;
        bind_text(self.database, statement.raw, 1, &id)
            .map_err(ScheduleMaterializationWriteError::Storage)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let record_json = column_text(statement.raw, 25)
                    .map_err(ScheduleMaterializationWriteError::Collision)?;
                let origin: ScheduleMaterializationOrigin = serde_json::from_str(&record_json)
                    .map_err(|error| {
                        ScheduleMaterializationWriteError::Collision(format!(
                            "deserialize materialization origin: {error}"
                        ))
                    })?;
                let indexed_saved_revision =
                    u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 4) }).map_err(
                        |_| {
                            ScheduleMaterializationWriteError::Collision(
                                "materialization origin saved-search revision is invalid".into(),
                            )
                        },
                    )?;
                if origin.occurrence_id
                    != column_text(statement.raw, 0)
                        .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.schedule_item_id != schedule_item_id
                    || origin.run_id
                        != column_text(statement.raw, 1)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.channel_id
                        != column_text(statement.raw, 2)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.saved_search_id
                        != column_text(statement.raw, 3)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.saved_search_revision != indexed_saved_revision
                    || origin.rule_id
                        != column_text(statement.raw, 5)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.rule_revision
                        != u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 6) })
                            .unwrap_or(0)
                    || origin.block_id
                        != column_text(statement.raw, 7)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.block_revision
                        != u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 8) })
                            .unwrap_or(0)
                    || origin.plan_window.starts_at_utc_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 9) }
                    || origin.plan_window.ends_at_utc_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 10) }
                    || origin.materialization_id
                        != column_text(statement.raw, 11)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.source_authority_sha256
                        != column_text(statement.raw, 12)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.source_input_sha256
                        != column_text(statement.raw, 13)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.candidate_projection_sha256
                        != column_text(statement.raw, 14)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.prior_airings_sha256
                        != column_text(statement.raw, 15)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.prior_airings_coverage.starts_at_utc_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 16) }
                    || origin.prior_airings_coverage.ends_at_utc_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 17) }
                    || origin.scope_sha256
                        != column_text(statement.raw, 18)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.original_schedule.revision
                        != u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 19) })
                            .unwrap_or(0)
                    || origin.original_schedule.asset_sha256
                        != column_text(statement.raw, 20)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.original_schedule.title
                        != column_text(statement.raw, 21)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.original_schedule.starts_at_utc_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 22) }
                    || origin.original_schedule.duration_ms
                        != u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 23) })
                            .unwrap_or(0)
                    || origin.original_schedule.state
                        != column_text(statement.raw, 24)
                            .map_err(ScheduleMaterializationWriteError::Collision)?
                    || origin.original_schedule.schedule_item_id != schedule_item_id
                    || origin.original_schedule.channel_id != origin.channel_id
                    || schedule_item_id_for_occurrence(&origin.occurrence_id) != schedule_item_id
                {
                    return Err(ScheduleMaterializationWriteError::Collision(
                        "materialization origin index and immutable record disagree".into(),
                    ));
                }
                Ok(Some(origin))
            }
            _ => Err(ScheduleMaterializationWriteError::Storage(sqlite_error(
                self.database,
            ))),
        }
    }

    pub fn read_schedule_materialization_run(
        &self,
        run_id: &str,
    ) -> Result<Option<ScheduleMaterializationRun>, String> {
        self.read_schedule_materialization_run_verified(run_id)
            .map_err(VerifiedMaterializationRunReadError::into_string)
    }

    /// The same complete immutable-run verifier used by the public reader,
    /// with SQLite faults retained separately for history's typed API error
    /// contract.
    fn read_schedule_materialization_run_verified(
        &self,
        run_id: &str,
    ) -> Result<Option<ScheduleMaterializationRun>, VerifiedMaterializationRunReadError> {
        #[cfg(test)]
        MATERIALIZATION_RUN_FULL_READS.with(|reads| reads.set(reads.get() + 1));
        if !is_canonical_uuid(run_id) {
            return Err("materialization run identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT run_id,request_sha256,result_sha256,window_starts_at_utc_ms,window_ends_at_utc_ms,observed_at_utc_ms,changed_by,occurred_at_unix_ms,plan_count,outcome_count FROM schedule_materialization_runs WHERE run_id=?1",
        )
        .map_err(VerifiedMaterializationRunReadError::Storage)?;
        let id = c_string(run_id, "materialization run identity")?;
        bind_text(self.database, statement.raw, 1, &id)
            .map_err(VerifiedMaterializationRunReadError::Storage)?;
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            return Ok(None);
        }
        if status != SQLITE_ROW {
            return Err(VerifiedMaterializationRunReadError::Storage(sqlite_error(
                self.database,
            )));
        }
        let stored_run_id = column_text(statement.raw, 0)?;
        let request_sha256 = column_text(statement.raw, 1)?;
        let result_sha256 = column_text(statement.raw, 2)?;
        let window = MaterializationWindow {
            starts_at_utc_ms: unsafe { sqlite3_column_int64(statement.raw, 3) },
            ends_at_utc_ms: unsafe { sqlite3_column_int64(statement.raw, 4) },
        };
        let observed_at_utc_ms = unsafe { sqlite3_column_int64(statement.raw, 5) };
        let changed_by = column_text(statement.raw, 6)?;
        let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 7) };
        let plan_count = usize::try_from(unsafe { sqlite3_column_int64(statement.raw, 8) })
            .map_err(|_| "materialization plan count is invalid".to_string())?;
        let outcome_count = usize::try_from(unsafe { sqlite3_column_int64(statement.raw, 9) })
            .map_err(|_| "materialization outcome count is invalid".to_string())?;
        drop(statement);

        let plans_statement = self.prepare(
            "SELECT ordinal,rule_id,channel_id,record_json FROM schedule_materialization_plans WHERE run_id=?1 ORDER BY ordinal LIMIT 65",
        )
        .map_err(VerifiedMaterializationRunReadError::Storage)?;
        bind_text(self.database, plans_statement.raw, 1, &id)
            .map_err(VerifiedMaterializationRunReadError::Storage)?;
        let mut plans = Vec::new();
        loop {
            match unsafe { sqlite3_step(plans_statement.raw) } {
                SQLITE_ROW => {
                    let ordinal =
                        u32::try_from(unsafe { sqlite3_column_int64(plans_statement.raw, 0) })
                            .map_err(|_| "materialization plan ordinal is invalid".to_string())?;
                    let indexed_rule = column_text(plans_statement.raw, 1)?;
                    let indexed_channel = column_text(plans_statement.raw, 2)?;
                    let record_json = column_text(plans_statement.raw, 3)?;
                    let record: ScheduleMaterializationPlanRecord =
                        serde_json::from_str(&record_json).map_err(|error| {
                            format!("deserialize materialization plan: {error}")
                        })?;
                    if record.ordinal != ordinal
                        || record.rule_id != indexed_rule
                        || record.channel_id != indexed_channel
                        || usize::try_from(record.ordinal).ok() != Some(plans.len())
                    {
                        return Err("materialization plan index and record disagree".into());
                    }
                    plans.push(record);
                }
                SQLITE_DONE => break,
                _ => {
                    return Err(VerifiedMaterializationRunReadError::Storage(sqlite_error(
                        self.database,
                    )));
                }
            }
        }

        let outcomes_statement = self.prepare(
            "SELECT ordinal,rule_id,channel_id,record_json FROM schedule_materialization_outcomes WHERE run_id=?1 ORDER BY ordinal LIMIT 4097",
        )
        .map_err(VerifiedMaterializationRunReadError::Storage)?;
        bind_text(self.database, outcomes_statement.raw, 1, &id)
            .map_err(VerifiedMaterializationRunReadError::Storage)?;
        let mut outcomes = Vec::new();
        loop {
            match unsafe { sqlite3_step(outcomes_statement.raw) } {
                SQLITE_ROW => {
                    let ordinal =
                        u32::try_from(unsafe { sqlite3_column_int64(outcomes_statement.raw, 0) })
                            .map_err(|_| "materialization outcome ordinal is invalid".to_string())?;
                    let indexed_rule = column_text(outcomes_statement.raw, 1)?;
                    let indexed_channel = column_text(outcomes_statement.raw, 2)?;
                    let record_json = column_text(outcomes_statement.raw, 3)?;
                    let record: ScheduleMaterializationOutcomeRecord =
                        serde_json::from_str(&record_json).map_err(|error| {
                            format!("deserialize materialization outcome: {error}")
                        })?;
                    if record.ordinal != ordinal
                        || record.rule_id != indexed_rule
                        || record.channel_id != indexed_channel
                        || usize::try_from(record.ordinal).ok() != Some(outcomes.len())
                    {
                        return Err("materialization outcome index and record disagree".into());
                    }
                    outcomes.push(record);
                }
                SQLITE_DONE => break,
                _ => {
                    return Err(VerifiedMaterializationRunReadError::Storage(sqlite_error(
                        self.database,
                    )));
                }
            }
        }
        if stored_run_id != run_id
            || !is_sha256_identity(&request_sha256)
            || !is_sha256_identity(&result_sha256)
            || plans.len() != plan_count
            || plans.is_empty()
            || plans.len() > MAX_BATCH_REQUESTS
            || outcomes.len() != outcome_count
            || outcomes.len() > MAX_BATCH_OUTCOMES
            || !is_canonical_uuid(&changed_by)
            || observed_at_utc_ms < 0
            || occurred_at_unix_ms <= 0
        {
            return Err("materialization run authority is malformed or incomplete".into());
        }
        let plans_by_rule = plans
            .iter()
            .map(|plan| (plan.rule_id.as_str(), plan))
            .collect::<BTreeMap<_, _>>();
        if plans_by_rule.len() != plans.len() {
            return Err("materialization run contains duplicate plan authority".into());
        }
        let maximum_plan_end = plans
            .iter()
            .map(|plan| plan.window.ends_at_utc_ms)
            .max()
            .ok_or("materialization run contains no plan window")?;
        if plans.iter().any(|plan| {
            plan.window.starts_at_utc_ms != window.starts_at_utc_ms
                || plan.window.ends_at_utc_ms <= plan.window.starts_at_utc_ms
                || plan.window.ends_at_utc_ms > window.ends_at_utc_ms
        }) || maximum_plan_end != window.ends_at_utc_ms
        {
            return Err("materialization plan windows do not span the exact batch union".into());
        }
        for record in &outcomes {
            let plan = plans_by_rule
                .get(record.rule_id.as_str())
                .ok_or("materialization outcome has no plan authority")?;
            if record.rule_revision != plan.rule_revision
                || record.channel_id != plan.channel_id
                || record.saved_search_id != plan.saved_search_id
                || record.saved_search_revision != plan.saved_search_revision
                || record.block_id != plan.block_id
                || record.block_revision != plan.block_revision
                || record.plan_window != plan.window
                || record.source_authority_sha256 != plan.source_authority_sha256
                || record.candidate_projection_sha256 != plan.candidate_projection_sha256
                || record.prior_airings_sha256 != plan.prior_airings_sha256
                || record.prior_airings_coverage != plan.prior_airings_coverage
                || record.scope_sha256 != plan.scope_sha256
                || record.source_input_sha256 != plan.source_input_sha256
            {
                return Err("materialization outcome and plan authority disagree".into());
            }
            validate_materialization_outcome_record(record, &plan.window)?;
        }
        let plan_digests = plan_digests_from_records(&plans)?;
        let semantic_outcomes = outcomes
            .iter()
            .map(|record| SemanticOutcomeDigest {
                ordinal: record.ordinal,
                rule_id: record.rule_id.clone(),
                rule_revision: record.rule_revision,
                channel_id: record.channel_id.clone(),
                block_id: record.block_id.clone(),
                block_revision: record.block_revision,
                source_authority_sha256: record.source_authority_sha256.clone(),
                outcome: record.compiled_outcome.clone(),
            })
            .collect::<Vec<_>>();
        let request_json = serde_json::to_vec(&BatchDigest {
            schema: "townlight.schedule-materialization-run.v1",
            window_starts_at_utc_ms: window.starts_at_utc_ms,
            window_ends_at_utc_ms: window.ends_at_utc_ms,
            plans: &plan_digests,
            outcomes: &semantic_outcomes,
        })
        .map_err(|error| format!("serialize stored materialization request: {error}"))?;
        let recomputed_request = lowercase_sha256(&request_json);
        let result_outcomes = result_outcome_digests(&outcomes);
        let result_json = serde_json::to_vec(&ResultDigest {
            schema: "townlight.schedule-materialization-result.v1",
            request_sha256: &request_sha256,
            outcomes: &result_outcomes,
        })
        .map_err(|error| format!("serialize stored materialization result: {error}"))?;
        let recomputed_result = lowercase_sha256(&result_json);
        if recomputed_request != request_sha256
            || recomputed_result != result_sha256
            || deterministic_uuid_v8("townlight.schedule-materialization-run.v1", &result_sha256)
                != run_id
        {
            return Err("materialization run digest or deterministic identity is invalid".into());
        }
        Ok(Some(ScheduleMaterializationRun {
            run_id: stored_run_id,
            request_sha256,
            result_sha256: result_sha256.clone(),
            batch_sha256: result_sha256,
            window,
            observed_at_utc_ms,
            changed_by,
            occurred_at_unix_ms,
            plans,
            outcomes,
        }))
    }
}

fn materialization_records(
    batch: &ArbitratedMaterializationBatch,
) -> Result<Vec<ScheduleMaterializationOutcomeRecord>, ScheduleMaterializationWriteError> {
    let mut records = Vec::new();
    for plan in batch.plans() {
        for outcome in &plan.outcomes {
            let ordinal = u32::try_from(records.len())
                .map_err(|_| ScheduleMaterializationWriteError::Capacity)?;
            let durable = durable_outcome(outcome);
            records.push(ScheduleMaterializationOutcomeRecord {
                ordinal,
                rule_id: plan.authority_binding().rule_id.clone(),
                rule_revision: plan.authority_binding().rule_revision,
                channel_id: plan.authority_binding().channel_id.clone(),
                saved_search_id: plan.authority_binding().saved_search_id.clone(),
                saved_search_revision: plan.authority_binding().saved_search_revision,
                block_id: plan.authority_binding().block_id.clone(),
                block_revision: plan.authority_binding().block_revision,
                plan_window: plan.authority_binding().window.clone(),
                materialization_id: plan
                    .draft_intent
                    .as_ref()
                    .map(|intent| intent.materialization_id.clone()),
                source_authority_sha256: plan.source_authority_sha256.clone(),
                candidate_projection_sha256: plan
                    .source_input_binding
                    .candidate_projection_sha256
                    .clone(),
                prior_airings_sha256: plan.source_input_binding.prior_airings_sha256.clone(),
                prior_airings_coverage: plan.source_input_binding.prior_airings_coverage.clone(),
                scope_sha256: plan.source_input_binding.scope_sha256.clone(),
                source_input_sha256: plan.source_input_binding.combined_sha256.clone(),
                compiled_outcome: durable.clone(),
                outcome: durable,
            });
        }
    }
    Ok(records)
}

fn plan_records(
    batch: &ArbitratedMaterializationBatch,
) -> Result<Vec<ScheduleMaterializationPlanRecord>, ScheduleMaterializationWriteError> {
    batch
        .plans()
        .iter()
        .enumerate()
        .map(|(ordinal, plan)| {
            let binding = plan.authority_binding();
            Ok(ScheduleMaterializationPlanRecord {
                ordinal: u32::try_from(ordinal)
                    .map_err(|_| ScheduleMaterializationWriteError::Capacity)?,
                rule_id: binding.rule_id.clone(),
                rule_revision: binding.rule_revision,
                channel_id: binding.channel_id.clone(),
                saved_search_id: binding.saved_search_id.clone(),
                saved_search_revision: binding.saved_search_revision,
                block_id: binding.block_id.clone(),
                block_revision: binding.block_revision,
                window: binding.window.clone(),
                source_authority_sha256: plan.source_authority_sha256.clone(),
                candidate_projection_sha256: plan
                    .source_input_binding
                    .candidate_projection_sha256
                    .clone(),
                candidate_receipt_sha256: plan
                    .source_input_binding
                    .candidate_receipt_sha256
                    .clone(),
                prior_airings_sha256: plan.source_input_binding.prior_airings_sha256.clone(),
                prior_receipt_sha256: plan
                    .source_input_binding
                    .prior_airings_receipt_sha256
                    .clone(),
                prior_airings_coverage: plan.source_input_binding.prior_airings_coverage.clone(),
                scope_sha256: plan.source_input_binding.scope_sha256.clone(),
                source_input_sha256: plan.source_input_binding.combined_sha256.clone(),
            })
        })
        .collect()
}

fn durable_outcome(outcome: &SlotMaterializationOutcome) -> ScheduleMaterializationOutcome {
    match outcome {
        SlotMaterializationOutcome::Proposed(occurrence) => {
            ScheduleMaterializationOutcome::Proposed {
                occurrence_id: occurrence.occurrence_id.clone(),
                schedule_item_id: schedule_item_id_for_occurrence(&occurrence.occurrence_id),
                asset_sha256: occurrence.asset_sha256.clone(),
                asset_title: occurrence.asset_title.clone(),
                starts_at_utc_ms: occurrence.starts_at_utc_ms,
                ends_at_utc_ms: occurrence.ends_at_utc_ms,
            }
        }
        SlotMaterializationOutcome::NoMatch { starts_at_utc_ms } => {
            ScheduleMaterializationOutcome::NoMatch {
                starts_at_utc_ms: *starts_at_utc_ms,
            }
        }
        SlotMaterializationOutcome::Blocked { starts_at_utc_ms } => {
            ScheduleMaterializationOutcome::Blocked {
                starts_at_utc_ms: *starts_at_utc_ms,
            }
        }
        SlotMaterializationOutcome::SuppressedByPrecedence {
            starts_at_utc_ms,
            winner_rule_id,
            candidate_priority,
            winner_priority,
            reason,
            ..
        } => ScheduleMaterializationOutcome::SuppressedByPrecedence {
            starts_at_utc_ms: *starts_at_utc_ms,
            winner_rule_id: winner_rule_id.clone(),
            candidate_priority: *candidate_priority,
            winner_priority: *winner_priority,
            reason: match reason {
                ArbitrationPrecedence::HigherPriority => "higher_priority",
                ArbitrationPrecedence::RuleIdTieBreak => "rule_id_tie_break",
            }
            .into(),
        },
    }
}

fn plan_digests(batch: &ArbitratedMaterializationBatch) -> Vec<PlanDigest> {
    batch
        .plans()
        .iter()
        .map(|plan: &MaterializationPlan| {
            let binding = plan.authority_binding();
            PlanDigest {
                rule_id: binding.rule_id.clone(),
                rule_revision: binding.rule_revision,
                channel_id: binding.channel_id.clone(),
                saved_search_id: binding.saved_search_id.clone(),
                saved_search_revision: binding.saved_search_revision,
                block_id: binding.block_id.clone(),
                block_revision: binding.block_revision,
                window_starts_at_utc_ms: binding.window.starts_at_utc_ms,
                window_ends_at_utc_ms: binding.window.ends_at_utc_ms,
                source_authority_sha256: plan.source_authority_sha256.clone(),
                candidate_projection_sha256: plan
                    .source_input_binding
                    .candidate_projection_sha256
                    .clone(),
                prior_airings_sha256: plan.source_input_binding.prior_airings_sha256.clone(),
                prior_coverage_starts_at_utc_ms: plan
                    .source_input_binding
                    .prior_airings_coverage
                    .starts_at_utc_ms,
                prior_coverage_ends_at_utc_ms: plan
                    .source_input_binding
                    .prior_airings_coverage
                    .ends_at_utc_ms,
                scope_sha256: plan.source_input_binding.scope_sha256.clone(),
            }
        })
        .collect()
}

fn plan_digests_from_records(
    plans: &[ScheduleMaterializationPlanRecord],
) -> Result<Vec<PlanDigest>, String> {
    plans
        .iter()
        .map(|plan| {
            if !is_canonical_uuid(&plan.rule_id)
                || !is_canonical_uuid(&plan.channel_id)
                || !is_canonical_uuid(&plan.saved_search_id)
                || !is_canonical_uuid(&plan.block_id)
                || plan.rule_revision == 0
                || plan.saved_search_revision == 0
                || plan.block_revision == 0
                || [
                    &plan.source_authority_sha256,
                    &plan.candidate_projection_sha256,
                    &plan.candidate_receipt_sha256,
                    &plan.prior_airings_sha256,
                    &plan.prior_receipt_sha256,
                    &plan.scope_sha256,
                    &plan.source_input_sha256,
                ]
                .into_iter()
                .any(|digest| !is_sha256_identity(digest))
            {
                return Err("materialization plan record is invalid".into());
            }
            Ok(PlanDigest {
                rule_id: plan.rule_id.clone(),
                rule_revision: plan.rule_revision,
                channel_id: plan.channel_id.clone(),
                saved_search_id: plan.saved_search_id.clone(),
                saved_search_revision: plan.saved_search_revision,
                block_id: plan.block_id.clone(),
                block_revision: plan.block_revision,
                window_starts_at_utc_ms: plan.window.starts_at_utc_ms,
                window_ends_at_utc_ms: plan.window.ends_at_utc_ms,
                source_authority_sha256: plan.source_authority_sha256.clone(),
                candidate_projection_sha256: plan.candidate_projection_sha256.clone(),
                prior_airings_sha256: plan.prior_airings_sha256.clone(),
                prior_coverage_starts_at_utc_ms: plan.prior_airings_coverage.starts_at_utc_ms,
                prior_coverage_ends_at_utc_ms: plan.prior_airings_coverage.ends_at_utc_ms,
                scope_sha256: plan.scope_sha256.clone(),
            })
        })
        .collect()
}

fn result_outcome_digests(
    records: &[ScheduleMaterializationOutcomeRecord],
) -> Vec<ResultOutcomeDigest> {
    records
        .iter()
        .map(|record| ResultOutcomeDigest {
            ordinal: record.ordinal,
            rule_id: record.rule_id.clone(),
            rule_revision: record.rule_revision,
            channel_id: record.channel_id.clone(),
            block_id: record.block_id.clone(),
            block_revision: record.block_revision,
            source_authority_sha256: record.source_authority_sha256.clone(),
            compiled_outcome: record.compiled_outcome.clone(),
            outcome: record.outcome.clone(),
        })
        .collect()
}

fn existing_schedule_snapshot(item: &ScheduleItem) -> ExistingScheduleSnapshot {
    ExistingScheduleSnapshot {
        schedule_item_id: item.item_id.clone(),
        revision: item.revision,
        channel_id: item.channel_id.clone(),
        asset_sha256: item.asset_id.clone(),
        title: item.title.clone(),
        starts_at_utc_ms: item.starts_at_unix_ms,
        duration_ms: item.duration_ms,
        state: schedule_state_name(item.state).into(),
    }
}

fn validate_materialization_outcome_record(
    record: &ScheduleMaterializationOutcomeRecord,
    window: &MaterializationWindow,
) -> Result<(), String> {
    if !is_canonical_uuid(&record.rule_id)
        || !is_canonical_uuid(&record.channel_id)
        || !is_canonical_uuid(&record.saved_search_id)
        || !is_canonical_uuid(&record.block_id)
        || record.rule_revision == 0
        || record.saved_search_revision == 0
        || record.block_revision == 0
        || record
            .materialization_id
            .as_deref()
            .is_some_and(|value| !is_sha256_identity(value))
        || [
            &record.source_authority_sha256,
            &record.candidate_projection_sha256,
            &record.prior_airings_sha256,
            &record.scope_sha256,
            &record.source_input_sha256,
        ]
        .into_iter()
        .any(|digest| !is_sha256_identity(digest))
        || record.prior_airings_coverage.starts_at_utc_ms < 0
        || record.prior_airings_coverage.ends_at_utc_ms
            <= record.prior_airings_coverage.starts_at_utc_ms
    {
        return Err("materialization outcome record is invalid".into());
    }
    validate_materialization_outcome(&record.compiled_outcome, window, true, &record.channel_id)?;
    validate_materialization_outcome(&record.outcome, window, false, &record.channel_id)
}

fn validate_materialization_outcome(
    outcome: &ScheduleMaterializationOutcome,
    window: &MaterializationWindow,
    compiled: bool,
    record_channel_id: &str,
) -> Result<(), String> {
    let validate_time = |time: i64| {
        (time >= window.starts_at_utc_ms && time < window.ends_at_utc_ms)
            .then_some(())
            .ok_or_else(|| "materialization outcome time escapes its run window".to_string())
    };
    match outcome {
        ScheduleMaterializationOutcome::Proposed {
            occurrence_id,
            schedule_item_id,
            asset_sha256,
            asset_title,
            starts_at_utc_ms,
            ends_at_utc_ms,
        } => {
            validate_time(*starts_at_utc_ms)?;
            if *ends_at_utc_ms <= *starts_at_utc_ms
                || *ends_at_utc_ms > window.ends_at_utc_ms
                || !is_sha256_identity(occurrence_id)
                || schedule_item_id_for_occurrence(occurrence_id) != *schedule_item_id
                || !is_sha256_identity(asset_sha256)
                || asset_title.trim().is_empty()
                || asset_title.len() > 240
                || asset_title.chars().any(char::is_control)
            {
                return Err("materialized Proposed outcome is invalid".into());
            }
        }
        ScheduleMaterializationOutcome::NoMatch { starts_at_utc_ms }
        | ScheduleMaterializationOutcome::Blocked { starts_at_utc_ms } => {
            validate_time(*starts_at_utc_ms)?;
        }
        ScheduleMaterializationOutcome::SuppressedByPrecedence {
            starts_at_utc_ms,
            winner_rule_id,
            reason,
            ..
        } => {
            validate_time(*starts_at_utc_ms)?;
            if !is_canonical_uuid(winner_rule_id)
                || !matches!(reason.as_str(), "higher_priority" | "rule_id_tie_break")
            {
                return Err("materialization precedence outcome is invalid".into());
            }
        }
        ScheduleMaterializationOutcome::ExistingAutomationDraftUnchanged {
            occurrence_id,
            schedule_item_id,
            existing,
        }
        | ScheduleMaterializationOutcome::ExistingAutomationDraftChanged {
            occurrence_id,
            schedule_item_id,
            existing,
        }
        | ScheduleMaterializationOutcome::ExistingCommitted {
            occurrence_id,
            schedule_item_id,
            existing,
        }
        | ScheduleMaterializationOutcome::CancelledOccurrence {
            occurrence_id,
            schedule_item_id,
            existing,
        }
        | ScheduleMaterializationOutcome::AlreadyAired {
            occurrence_id,
            schedule_item_id,
            existing,
        } => {
            validate_existing_outcome(occurrence_id, schedule_item_id, existing, record_channel_id)?
        }
        ScheduleMaterializationOutcome::SuppressedByExistingSchedule {
            occurrence_id,
            schedule_item_id,
            existing,
            existing_owner,
        } => {
            if !matches!(
                existing_owner.as_str(),
                "manual" | "automation" | "committed"
            ) {
                return Err("materialization overlap owner is invalid".into());
            }
            validate_existing_outcome(
                occurrence_id,
                schedule_item_id,
                existing,
                record_channel_id,
            )?;
        }
    }
    if compiled
        && matches!(
            outcome,
            ScheduleMaterializationOutcome::ExistingAutomationDraftUnchanged { .. }
                | ScheduleMaterializationOutcome::ExistingAutomationDraftChanged { .. }
                | ScheduleMaterializationOutcome::ExistingCommitted { .. }
                | ScheduleMaterializationOutcome::CancelledOccurrence { .. }
                | ScheduleMaterializationOutcome::AlreadyAired { .. }
                | ScheduleMaterializationOutcome::SuppressedByExistingSchedule { .. }
        )
    {
        return Err("compiled materialization outcome contains a storage disposition".into());
    }
    Ok(())
}

fn validate_existing_outcome(
    occurrence_id: &str,
    schedule_item_id: &str,
    existing: &ExistingScheduleSnapshot,
    record_channel_id: &str,
) -> Result<(), String> {
    if !is_sha256_identity(occurrence_id)
        || schedule_item_id_for_occurrence(occurrence_id) != schedule_item_id
        || !is_canonical_uuid(&existing.schedule_item_id)
        || existing.schedule_item_id != schedule_item_id
        || existing.revision == 0
        || !is_canonical_uuid(&existing.channel_id)
        || existing.channel_id != record_channel_id
        || !is_sha256_identity(&existing.asset_sha256)
        || existing.title.trim().is_empty()
        || existing.title.len() > 240
        || existing.title.chars().any(char::is_control)
        || existing.starts_at_utc_ms < 0
        || existing.duration_ms == 0
        || !matches!(existing.state.as_str(), "draft" | "committed" | "cancelled")
    {
        return Err("materialization existing-schedule disposition is invalid".into());
    }
    Ok(())
}

fn lowercase_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn station_clock_utc_ms() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "station clock is before the Unix epoch".to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "station clock exceeds signed milliseconds".to_string())
}

fn second_upcoming_utc_minute(
    observed_at_utc_ms: i64,
) -> Result<i64, ScheduleMaterializationWriteError> {
    if observed_at_utc_ms < 0 {
        return Err(ScheduleMaterializationWriteError::Invalid(
            "materialization authority time is before the Unix epoch".into(),
        ));
    }
    observed_at_utc_ms
        .checked_div(UTC_MINUTE_MS)
        .and_then(|minute| minute.checked_add(2))
        .and_then(|minute| minute.checked_mul(UTC_MINUTE_MS))
        .ok_or_else(|| {
            ScheduleMaterializationWriteError::Invalid(
                "materialization authority time cannot produce a bounded future minute".into(),
            )
        })
}

fn materialization_window_for_days(
    starts_at_utc_ms: i64,
    rolling_window_days: u8,
) -> Result<MaterializationWindow, ScheduleMaterializationWriteError> {
    let duration_ms = i64::from(rolling_window_days)
        .checked_mul(UTC_DAY_MS)
        .ok_or_else(|| {
            ScheduleMaterializationWriteError::Invalid(
                "materialization rolling horizon exceeds signed milliseconds".into(),
            )
        })?;
    let ends_at_utc_ms = starts_at_utc_ms.checked_add(duration_ms).ok_or_else(|| {
        ScheduleMaterializationWriteError::Invalid(
            "materialization rolling horizon exceeds signed UTC time".into(),
        )
    })?;
    let window = MaterializationWindow {
        starts_at_utc_ms,
        ends_at_utc_ms,
    };
    if window.starts_at_utc_ms < 0 || window.ends_at_utc_ms <= window.starts_at_utc_ms {
        return Err(ScheduleMaterializationWriteError::Invalid(
            "materialization rolling horizon is empty or invalid".into(),
        ));
    }
    Ok(window)
}

fn deterministic_uuid_v8(namespace: &str, identity: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(namespace.as_bytes());
    digest.update([0]);
    digest.update(identity.as_bytes());
    let mut bytes: [u8; 16] = digest.finalize()[..16]
        .try_into()
        .expect("fixed digest size");
    bytes[6] = (bytes[6] & 0x0f) | 0x80;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
}

fn schedule_item_id_for_occurrence(occurrence_id: &str) -> String {
    let mut digest = Sha256::new();
    digest.update("townlight.schedule-item-id.v1".as_bytes());
    digest.update(1_u16.to_le_bytes());
    digest.update((occurrence_id.len() as u64).to_le_bytes());
    digest.update(occurrence_id.as_bytes());
    let mut bytes: [u8; 16] = digest.finalize()[..16]
        .try_into()
        .expect("fixed digest size");
    bytes[6] = (bytes[6] & 0x0f) | 0x80;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
}

fn finish_materialization_transaction<T>(
    store: &StationStore,
    outcome: Result<T, ScheduleMaterializationWriteError>,
) -> Result<T, ScheduleMaterializationWriteError> {
    match outcome {
        Ok(value) => match store.execute("COMMIT") {
            Ok(()) => Ok(value),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(ScheduleMaterializationWriteError::Storage(error))
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

fn finish_materialization_history_transaction<T>(
    store: &StationStore,
    outcome: Result<T, ScheduleMaterializationHistoryReadError>,
) -> Result<T, ScheduleMaterializationHistoryReadError> {
    match outcome {
        Ok(value) => match store.execute("COMMIT") {
            Ok(()) => Ok(value),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(ScheduleMaterializationHistoryReadError::Storage(error))
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

fn materialization_write_error_into_history(
    error: ScheduleMaterializationWriteError,
) -> ScheduleMaterializationHistoryReadError {
    match error {
        ScheduleMaterializationWriteError::Storage(error) => {
            ScheduleMaterializationHistoryReadError::Storage(error)
        }
        ScheduleMaterializationWriteError::Capacity => {
            ScheduleMaterializationHistoryReadError::Capacity
        }
        ScheduleMaterializationWriteError::Invalid(error)
        | ScheduleMaterializationWriteError::StaleAuthority(error)
        | ScheduleMaterializationWriteError::Collision(error) => {
            ScheduleMaterializationHistoryReadError::Integrity(format!(
                "materialization history Draft provenance: {error}"
            ))
        }
    }
}

fn extend_materialization_history_aggregate(
    total_outcomes: &mut usize,
    total_drafts: &mut usize,
    outcomes: usize,
    drafts: usize,
) -> Result<(), ScheduleMaterializationHistoryReadError> {
    *total_outcomes = total_outcomes
        .checked_add(outcomes)
        .ok_or(ScheduleMaterializationHistoryReadError::Capacity)?;
    *total_drafts = total_drafts
        .checked_add(drafts)
        .ok_or(ScheduleMaterializationHistoryReadError::Capacity)?;
    if *total_outcomes > MAX_SCHEDULE_MATERIALIZATION_HISTORY_OUTCOMES
        || *total_drafts > MAX_SCHEDULE_MATERIALIZATION_HISTORY_DRAFTS
    {
        return Err(ScheduleMaterializationHistoryReadError::Capacity);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
    use station_schedule::{AssetReadiness, MediaAsset};
    use station_schedule_automation::{
        AssetPickStrategy, AssetPredicate, LocalDate, RecurrenceTimeZone, SavedSearchOrder,
        SavedSearchQuery, SourceEvidenceReceipt, SourceEvidenceVerifier,
    };

    use super::*;

    const HISTORY_TEST_CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
    const HISTORY_TEST_OWNER: &str = "22222222-2222-4222-8222-222222222222";
    const HISTORY_TEST_SEARCH: &str = "33333333-3333-4333-8333-333333333333";
    const HISTORY_TEST_BLOCK: &str = "44444444-4444-4444-8444-444444444444";
    const HISTORY_TEST_RULE: &str = "55555555-5555-4555-8555-555555555555";
    const HISTORY_TEST_ASSET: &str =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    struct HistoryTestAuthority {
        now: i64,
        sequence: AtomicU64,
    }

    impl SourceEvidenceVerifier for HistoryTestAuthority {
        fn verify(
            &self,
            _kind: SourceEvidenceKind,
            receipt: &SourceEvidenceReceipt,
            scope_sha256: &str,
            payload_sha256: &str,
            observed_at_utc_ms: i64,
        ) -> Result<(), String> {
            if observed_at_utc_ms != self.now
                || receipt.scope_sha256 != scope_sha256
                || receipt.attestation_sha256 != payload_sha256
            {
                return Err("history test source evidence did not authenticate".into());
            }
            Ok(())
        }
    }

    impl SourceEvidenceAuthority for HistoryTestAuthority {
        fn observed_at_utc_ms(&self) -> Result<i64, String> {
            Ok(self.now)
        }

        fn issue_source_evidence(
            &self,
            _kind: SourceEvidenceKind,
            scope_sha256: &str,
            payload_sha256: &str,
            lifetime_ms: i64,
        ) -> Result<SourceEvidenceReceipt, String> {
            let sequence = self.sequence.fetch_add(1, Ordering::Relaxed);
            Ok(SourceEvidenceReceipt {
                issuer_id: "66666666-6666-4666-8666-666666666666".into(),
                receipt_id: format!("77777777-7777-4777-8777-{sequence:012}"),
                issued_at_utc_ms: self.now,
                expires_at_utc_ms: self.now + lifetime_ms,
                scope_sha256: scope_sha256.into(),
                attestation_sha256: payload_sha256.into(),
            })
        }
    }

    fn history_test_database() -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time is after Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-history-origin-read-{nonce}.db"))
    }

    fn cleanup_history_test_database(path: &std::path::Path) {
        for candidate in [
            path.to_path_buf(),
            std::path::PathBuf::from(format!("{}-wal", path.display())),
            std::path::PathBuf::from(format!("{}-shm", path.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    fn seed_history_origin_read_fixture(store: &StationStore) {
        let search = SavedSearch {
            search_id: HISTORY_TEST_SEARCH.into(),
            name: "Council programs".into(),
            owner_operator_id: HISTORY_TEST_OWNER.into(),
            created_at_utc_ms: 1,
            query: SavedSearchQuery {
                all_of: vec![AssetPredicate::TitleContains("Council".into())],
                any_of: vec![],
                order_by: SavedSearchOrder::TitleAscending,
                limit: 8,
            },
            revision: 1,
        };
        let block = DaypartBlock {
            block_id: HISTORY_TEST_BLOCK.into(),
            channel_id: HISTORY_TEST_CHANNEL.into(),
            label: "Full day".into(),
            starts_local_minute: 0,
            ends_local_minute: 1_440,
            weekday_mask: 0b0111_1111,
            season_starts_on: None,
            season_ends_on: None,
            allow_rule_ids: vec![HISTORY_TEST_RULE.into()],
            revision: 1,
        };
        let rule = RecurringRule {
            rule_id: HISTORY_TEST_RULE.into(),
            channel_id: HISTORY_TEST_CHANNEL.into(),
            saved_search_id: HISTORY_TEST_SEARCH.into(),
            saved_search_revision: 1,
            pick_strategy: AssetPickStrategy::Top,
            rolling_window_days: 60,
            avoid_repeat_within_days: None,
            enabled: true,
            priority: 0,
            block_id: HISTORY_TEST_BLOCK.into(),
            block_revision: 1,
            weekday_mask: 0b0111_1111,
            interval_weeks: 1,
            anchor_date: LocalDate {
                year: 2099,
                month: 1,
                day: 1,
            },
            timezone: RecurrenceTimeZone::Utc,
            local_start_minute: 0,
            duration_minutes: 60,
            revision: 1,
        };
        store
            .write_saved_search(&search, None, HISTORY_TEST_OWNER, 1)
            .expect("write history test saved search");
        store
            .write_daypart_block(&block, None, HISTORY_TEST_OWNER, 2)
            .expect("write history test daypart block");
        store
            .write_recurring_rule(&rule, None, HISTORY_TEST_OWNER, 3)
            .expect("write history test recurring rule");
        store
            .put_media_asset(&MediaAsset {
                asset_id: HISTORY_TEST_ASSET.into(),
                media_path: "C:\\station\\history-origin-read.ts".into(),
                duration_ms: 3_600_000,
                readiness: AssetReadiness::Ready,
            })
            .expect("write history test media asset");
        let draft = CatalogAssetRecord {
            asset_sha256: HISTORY_TEST_ASSET.into(),
            title: "Council meeting".into(),
            meeting_body: Some("City Council".into()),
            editorial_state: CatalogEditorialState::Draft,
            published_at_utc_ms: None,
            revision: 1,
        };
        store
            .write_catalog_asset(HISTORY_TEST_ASSET, &draft, None, HISTORY_TEST_OWNER, 4)
            .expect("write history test catalog draft");
        let published = CatalogAssetRecord {
            editorial_state: CatalogEditorialState::Published,
            published_at_utc_ms: Some(5),
            revision: 2,
            ..draft
        };
        store
            .write_catalog_asset(
                HISTORY_TEST_ASSET,
                &published,
                Some(1),
                HISTORY_TEST_OWNER,
                5,
            )
            .expect("write history test published catalog asset");
    }

    #[test]
    fn derived_materialization_window_starts_at_second_upcoming_minute() {
        assert_eq!(
            second_upcoming_utc_minute(4_070_908_800_001).unwrap(),
            4_070_908_920_000
        );
        assert_eq!(
            materialization_window_for_days(4_070_908_920_000, 14).unwrap(),
            MaterializationWindow {
                starts_at_utc_ms: 4_070_908_920_000,
                ends_at_utc_ms: 4_072_118_520_000,
            }
        );
    }

    #[test]
    fn derived_materialization_window_fails_closed_on_clock_or_horizon_overflow() {
        assert!(matches!(
            second_upcoming_utc_minute(-1),
            Err(ScheduleMaterializationWriteError::Invalid(_))
        ));
        assert!(matches!(
            second_upcoming_utc_minute(i64::MAX),
            Err(ScheduleMaterializationWriteError::Invalid(_))
        ));
        assert!(matches!(
            materialization_window_for_days(i64::MAX - UTC_DAY_MS, 2),
            Err(ScheduleMaterializationWriteError::Invalid(_))
        ));
    }

    #[test]
    fn history_aggregate_cap_rejects_excess_and_integer_overflow() {
        let mut outcomes = MAX_SCHEDULE_MATERIALIZATION_HISTORY_OUTCOMES - 1;
        let mut drafts = MAX_SCHEDULE_MATERIALIZATION_HISTORY_DRAFTS - 1;
        assert!(matches!(
            extend_materialization_history_aggregate(&mut outcomes, &mut drafts, 2, 0),
            Err(ScheduleMaterializationHistoryReadError::Capacity)
        ));
        let mut outcomes = usize::MAX;
        let mut drafts = 0;
        assert!(matches!(
            extend_materialization_history_aggregate(&mut outcomes, &mut drafts, 1, 0),
            Err(ScheduleMaterializationHistoryReadError::Capacity)
        ));
    }

    #[test]
    fn history_reads_a_maximum_all_proposed_run_once_not_once_per_draft() {
        let path = history_test_database();
        let store = StationStore::open(&path).expect("open history test database");
        seed_history_origin_read_fixture(&store);
        let now = i64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system time is after Unix epoch")
                .as_millis(),
        )
        .expect("current milliseconds fit signed integer");
        let authority = HistoryTestAuthority {
            now,
            sequence: AtomicU64::new(0),
        };
        let run = store
            .materialize_schedule_batch_with_explicit_window(
                HISTORY_TEST_CHANNEL,
                &MaterializationWindow {
                    starts_at_utc_ms: 4_070_908_800_000,
                    ends_at_utc_ms: 4_070_908_800_000 + 60 * UTC_DAY_MS,
                },
                HISTORY_TEST_OWNER,
                &authority,
            )
            .expect("materialize maximum rolling window");
        let ScheduleMaterializationApplyOutcome::Applied(run) = run else {
            panic!("fixture materialization must create a fresh run");
        };
        assert_eq!(run.outcomes.len(), 60);
        assert!(run.outcomes.iter().all(|record| matches!(
            record.outcome,
            ScheduleMaterializationOutcome::Proposed { .. }
        )));

        MATERIALIZATION_RUN_FULL_READS.with(|reads| reads.set(0));
        let page = store
            .list_schedule_materialization_history(HISTORY_TEST_CHANNEL, 1, None)
            .expect("read maximum all-Proposed history run");
        assert_eq!(page.entries.len(), 1);
        assert_eq!(page.entries[0].run.run_id, run.run_id);
        assert_eq!(page.entries[0].generated_drafts.len(), 60);
        MATERIALIZATION_RUN_FULL_READS.with(|reads| {
            assert_eq!(
                reads.get(),
                1,
                "history must validate the already reconstructed creator run instead of rereading it for every Draft"
            );
        });

        drop(store);
        cleanup_history_test_database(&path);
    }

    #[test]
    fn existing_outcomes_bind_the_nested_schedule_identity_and_channel_to_the_record() {
        let occurrence_id = "a".repeat(64);
        let schedule_item_id = schedule_item_id_for_occurrence(&occurrence_id);
        let channel_id = "11111111-1111-4111-8111-111111111111".to_string();
        let existing = ExistingScheduleSnapshot {
            schedule_item_id: schedule_item_id.clone(),
            revision: 1,
            channel_id: channel_id.clone(),
            asset_sha256: "b".repeat(64),
            title: "Existing schedule row".into(),
            starts_at_utc_ms: 0,
            duration_ms: 1_000,
            state: "draft".into(),
        };
        let mut record = ScheduleMaterializationOutcomeRecord {
            ordinal: 0,
            rule_id: "22222222-2222-4222-8222-222222222222".into(),
            rule_revision: 1,
            channel_id: channel_id.clone(),
            saved_search_id: "33333333-3333-4333-8333-333333333333".into(),
            saved_search_revision: 1,
            block_id: "44444444-4444-4444-8444-444444444444".into(),
            block_revision: 1,
            plan_window: MaterializationWindow {
                starts_at_utc_ms: 0,
                ends_at_utc_ms: 1_000,
            },
            materialization_id: None,
            source_authority_sha256: "c".repeat(64),
            candidate_projection_sha256: "d".repeat(64),
            prior_airings_sha256: "e".repeat(64),
            prior_airings_coverage: MaterializationWindow {
                starts_at_utc_ms: 0,
                ends_at_utc_ms: 1_000,
            },
            scope_sha256: "f".repeat(64),
            source_input_sha256: "0".repeat(64),
            compiled_outcome: ScheduleMaterializationOutcome::NoMatch {
                starts_at_utc_ms: 0,
            },
            outcome: ScheduleMaterializationOutcome::ExistingAutomationDraftChanged {
                occurrence_id: occurrence_id.clone(),
                schedule_item_id: schedule_item_id.clone(),
                existing: existing.clone(),
            },
        };
        assert!(validate_materialization_outcome_record(&record, &record.plan_window).is_ok());

        {
            let ScheduleMaterializationOutcome::ExistingAutomationDraftChanged { existing, .. } =
                &mut record.outcome
            else {
                panic!("fixture must remain existing-bearing");
            };
            existing.channel_id = "99999999-9999-4999-8999-999999999999".into();
        }
        assert!(validate_materialization_outcome_record(&record, &record.plan_window).is_err());
        {
            let ScheduleMaterializationOutcome::ExistingAutomationDraftChanged { existing, .. } =
                &mut record.outcome
            else {
                panic!("fixture must remain existing-bearing");
            };
            existing.channel_id = channel_id;
            existing.schedule_item_id = "88888888-8888-4888-8888-888888888888".into();
        }
        assert!(validate_materialization_outcome_record(&record, &record.plan_window).is_err());
    }
}
