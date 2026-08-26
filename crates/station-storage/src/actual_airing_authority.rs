use super::*;

/// Maximum number of immutable worker-confirmed as-run facts retained by one
/// station database.  The bound is intentionally finite so a corrupt or
/// hostile writer cannot turn a prior-airing read into an unbounded query.
pub const MAX_ACTUAL_AIRINGS: usize = 100_000;
pub const MAX_ACTUAL_AIRING_QUERY: usize = 128;
/// S19 permits a 60-day materialization window and up to a 60-day repeat
/// lookback, so one complete prior-airing projection never spans more than
/// 120 days.
pub const MAX_ACTUAL_AIRING_QUERY_WINDOW_MS: i64 = 120 * 24 * 60 * 60 * 1_000;
/// A local worker observation may lead the station-owned receive clock only
/// by this small scheduling/clock tolerance.
pub const MAX_ACTUAL_AIRING_FUTURE_SKEW_MS: i64 = 5_000;
/// First ingestion must arrive within one day of the claimed on-air instant.
/// Idempotent retries remain accepted later because the first trusted receive
/// time is immutable once recorded.
pub const MAX_ACTUAL_AIRING_RECEIVE_DELAY_MS: i64 = 24 * 60 * 60 * 1_000;

const CREATE_ACTUAL_AIRINGS_TABLE: &str = "CREATE TABLE IF NOT EXISTS actual_airings (\
       airing_id TEXT PRIMARY KEY CHECK (length(airing_id)=36),\
       channel_id TEXT NOT NULL CHECK (length(channel_id)=36),\
       asset_sha256 TEXT NOT NULL CHECK (length(asset_sha256)=64),\
       schedule_item_id TEXT NOT NULL DEFAULT '',\
       schedule_item_revision INTEGER NOT NULL DEFAULT -1 CHECK (schedule_item_revision >= -1),\
       report_id TEXT NOT NULL UNIQUE CHECK (length(report_id) BETWEEN 1 AND 200 AND report_id=trim(report_id)),\
       aired_at_utc_ms INTEGER NOT NULL CHECK (aired_at_utc_ms >= 0),\
       media_offset_ms INTEGER NOT NULL CHECK (media_offset_ms >= 0),\
       received_at_utc_ms INTEGER NOT NULL CHECK (received_at_utc_ms >= 0),\
       worker_boot_id TEXT NOT NULL CHECK (length(worker_boot_id)=36),\
       worker_sequence INTEGER NOT NULL CHECK (worker_sequence >= 1),\
       worker_id TEXT NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 128 AND worker_id=trim(worker_id)),\
       worker_ack_id TEXT NOT NULL UNIQUE CHECK (length(worker_ack_id)=36),\
       record_json TEXT NOT NULL,\
       CHECK ((schedule_item_id='' AND schedule_item_revision=-1) OR \
              (length(schedule_item_id)=36 AND schedule_item_revision>=1))\
     )";

/// Untrusted worker observation. Receive time is deliberately absent: the
/// standalone storage boundary derives it from the station clock, while the
/// atomic transaction seam accepts it only as an explicitly trusted argument.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActualAiringObservation {
    pub airing_id: String,
    pub channel_id: String,
    pub asset_sha256: String,
    pub schedule_item_id: Option<String>,
    pub schedule_item_revision: Option<u64>,
    pub report_id: String,
    pub aired_at_utc_ms: i64,
    pub media_offset_ms: u64,
    pub worker_boot_id: String,
    pub worker_sequence: u64,
    pub worker_id: String,
    pub worker_ack_id: String,
}

/// A worker-confirmed fact that an asset actually went on air.
///
/// This is deliberately not a schedule or approval projection.  `aired_at`
/// is the actual observed on-air time. `worker_boot_id` plus
/// `worker_sequence`, `worker_ack_id`, and `report_id` are immutable
/// acknowledgement/event identities used to make retries/replays idempotent
/// and reject a second nonidentical fact for one accepted report.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActualAiringFact {
    pub airing_id: String,
    pub channel_id: String,
    pub asset_sha256: String,
    pub schedule_item_id: Option<String>,
    pub schedule_item_revision: Option<u64>,
    pub report_id: String,
    pub aired_at_utc_ms: i64,
    #[serde(default)]
    pub media_offset_ms: u64,
    pub received_at_utc_ms: i64,
    pub worker_boot_id: String,
    pub worker_sequence: u64,
    pub worker_id: String,
    pub worker_ack_id: String,
}

/// Protocol-neutral worker acknowledgement for one committed scheduled take.
///
/// Station receive and runtime-heartbeat time are deliberately absent. The
/// storage authority derives both from its own clock while holding the write
/// transaction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduledTakeAcknowledgement {
    pub airing_id: String,
    pub report_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub schedule_item_revision: u64,
    pub asset_sha256: String,
    pub aired_at_utc_ms: i64,
    pub media_offset_ms: u64,
    pub worker_boot_id: String,
    pub worker_sequence: u64,
    pub worker_id: String,
    pub worker_ack_id: String,
}

/// Full worker acknowledgement binding before deterministic event identities
/// are derived. Station receive time is intentionally not part of this value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduledTakeAcknowledgementBinding {
    pub report_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub schedule_item_revision: u64,
    pub asset_sha256: String,
    pub aired_at_utc_ms: i64,
    pub media_offset_ms: u64,
    pub worker_boot_id: String,
    pub worker_sequence: u64,
    pub worker_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScheduledTakeApplyOutcome {
    /// The fact, dispatch acknowledgement, and current runtime observation
    /// were committed together while the scheduled window was still current.
    Applied,
    /// The fact and dispatch acknowledgement were committed, but the station
    /// received the worker confirmation after its committed schedule window.
    /// An old fact must never replace the current on-air runtime projection.
    AppliedHistorical,
    AlreadyApplied,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduledTakeApplyError {
    Invalid(String),
    NotFound,
    Conflict(String),
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ActualAiringWriteError {
    Invalid(String),
    Conflict(String),
    TransactionRequired,
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActualAiringWriteOutcome {
    Inserted,
    AlreadyRecorded,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ActualAiringReadError {
    Invalid(String),
    Corrupt(String),
    Capacity,
    Storage(String),
}

impl StationStore {
    pub(super) fn initialize_actual_airing_authority_schema(&self) -> Result<(), String> {
        self.execute(CREATE_ACTUAL_AIRINGS_TABLE)?;
        self.migrate_actual_airing_media_offset()?;
        self.migrate_actual_airing_report_id_constraint()?;
        self.execute(
            "CREATE INDEX IF NOT EXISTS actual_airings_channel_time_order \
               ON actual_airings(channel_id, asset_sha256, aired_at_utc_ms, airing_id);\
             CREATE INDEX IF NOT EXISTS actual_airings_channel_time_window \
               ON actual_airings(channel_id, aired_at_utc_ms, asset_sha256, airing_id);\
             CREATE INDEX IF NOT EXISTS actual_airings_worker_ack \
               ON actual_airings(worker_ack_id);\
             CREATE INDEX IF NOT EXISTS actual_airings_schedule_item \
               ON actual_airings(schedule_item_id, schedule_item_revision);\
             CREATE UNIQUE INDEX IF NOT EXISTS actual_airings_worker_sequence \
               ON actual_airings(channel_id, worker_boot_id, worker_sequence);\
             CREATE TRIGGER IF NOT EXISTS actual_airings_capacity \
               BEFORE INSERT ON actual_airings \
               WHEN (SELECT COUNT(*) FROM actual_airings) >= 100000 \
               BEGIN SELECT RAISE(ABORT, 'actual airing authority capacity exceeded'); END;\
             CREATE TRIGGER IF NOT EXISTS actual_airings_no_update \
               BEFORE UPDATE ON actual_airings BEGIN \
                 SELECT RAISE(ABORT, 'actual airing authority is append-only'); \
               END;\
             CREATE TRIGGER IF NOT EXISTS actual_airings_no_delete \
               BEFORE DELETE ON actual_airings BEGIN \
                 SELECT RAISE(ABORT, 'actual airing authority is append-only'); \
               END;",
        )
    }

    fn migrate_actual_airing_media_offset(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('actual_airings') \
             WHERE name='media_offset_ms'",
        )?;
        let has_media_offset = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !has_media_offset {
            self.execute(
                "ALTER TABLE actual_airings ADD COLUMN \
                 media_offset_ms INTEGER NOT NULL DEFAULT 0 CHECK (media_offset_ms >= 0)",
            )?;
        }
        Ok(())
    }

    fn migrate_actual_airing_report_id_constraint(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='actual_airings'",
        )?;
        let schema = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("actual airing schema was not created".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        let compact = schema.split_whitespace().collect::<String>();
        if !compact.contains("CHECK(length(report_id)=36)") {
            return Ok(());
        }

        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.execute(
                "DROP TRIGGER IF EXISTS actual_airings_capacity;\
                 DROP TRIGGER IF EXISTS actual_airings_no_update;\
                 DROP TRIGGER IF EXISTS actual_airings_no_delete;\
                 DROP INDEX IF EXISTS actual_airings_channel_time_order;\
                 DROP INDEX IF EXISTS actual_airings_channel_time_window;\
                 DROP INDEX IF EXISTS actual_airings_s19_identity;\
                 DROP INDEX IF EXISTS actual_airings_worker_ack;\
                 DROP INDEX IF EXISTS actual_airings_worker_sequence;",
            )?;
            self.execute("ALTER TABLE actual_airings RENAME TO actual_airings_legacy_report_id")?;
            self.execute(CREATE_ACTUAL_AIRINGS_TABLE)?;
            self.execute(
                "INSERT INTO actual_airings(\
                   airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                   report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                   worker_id,worker_ack_id,record_json) \
                 SELECT airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                   report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                   worker_id,worker_ack_id,record_json \
                 FROM actual_airings_legacy_report_id",
            )?;
            self.execute("DROP TABLE actual_airings_legacy_report_id")
        })();
        self.finish_transaction(outcome)
    }

    /// Appends one worker observation in its own `BEGIN IMMEDIATE`
    /// transaction, deriving immutable receive time from the station clock.
    ///
    /// Scheduled takes must use `apply_scheduled_take_acknowledgement` so the
    /// receipt, dispatch CAS, and runtime observation share one transaction.
    /// This wrapper remains for non-scheduled standalone worker ingestion.
    pub fn append_actual_airing(
        &self,
        observation: &ActualAiringObservation,
    ) -> Result<ActualAiringWriteOutcome, ActualAiringWriteError> {
        let received_at_utc_ms = station_utc_ms().map_err(ActualAiringWriteError::Storage)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ActualAiringWriteError::Storage)?;
        let outcome = self.append_actual_airing_in_transaction(observation, received_at_utc_ms);
        match outcome {
            Ok(value) => match self.execute("COMMIT") {
                Ok(()) => Ok(value),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ActualAiringWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    /// Appends one fact without beginning or committing a transaction.
    ///
    /// The caller must already own this station connection's active SQLite
    /// write transaction, must derive `trusted_received_at_utc_ms` from the
    /// station clock rather than worker input, and must commit or roll back.
    /// This no-BEGIN seam remains available to storage-owned transaction
    /// composition; external callers cannot use it from autocommit mode.
    pub fn append_actual_airing_in_transaction(
        &self,
        observation: &ActualAiringObservation,
        trusted_received_at_utc_ms: i64,
    ) -> Result<ActualAiringWriteOutcome, ActualAiringWriteError> {
        self.append_actual_airing_in_transaction_with_receive_policy(
            observation,
            trusted_received_at_utc_ms,
            true,
        )
    }

    fn append_actual_airing_in_transaction_with_receive_policy(
        &self,
        observation: &ActualAiringObservation,
        trusted_received_at_utc_ms: i64,
        enforce_first_ingestion_delay: bool,
    ) -> Result<ActualAiringWriteOutcome, ActualAiringWriteError> {
        if unsafe { sqlite3_get_autocommit(self.database) } != 0 {
            return Err(ActualAiringWriteError::TransactionRequired);
        }
        validate_actual_airing_observation(observation).map_err(ActualAiringWriteError::Invalid)?;

        let mut existing = Vec::new();
        for candidate in [
            self.read_actual_airing_key("airing_id", &observation.airing_id),
            self.read_actual_airing_key("worker_ack_id", &observation.worker_ack_id),
            self.read_actual_airing_key("report_id", &observation.report_id),
            self.read_actual_airing_worker_sequence(
                &observation.channel_id,
                &observation.worker_boot_id,
                observation.worker_sequence,
            ),
        ] {
            if let Some(record) = candidate.map_err(ActualAiringWriteError::Storage)?
                && !existing
                    .iter()
                    .any(|item: &ActualAiringFact| item == &record)
            {
                existing.push(record);
            }
        }
        if let Some(record) = existing.first() {
            if existing.iter().any(|item| item != record)
                || !record.matches_observation(observation)
            {
                return Err(ActualAiringWriteError::Conflict(
                    "actual-airing identity is already bound to a different fact".into(),
                ));
            }
            return Ok(ActualAiringWriteOutcome::AlreadyRecorded);
        }

        if enforce_first_ingestion_delay {
            validate_trusted_receive_time(observation.aired_at_utc_ms, trusted_received_at_utc_ms)
                .map_err(ActualAiringWriteError::Invalid)?;
        } else {
            validate_trusted_receive_time_allow_late(
                observation.aired_at_utc_ms,
                trusted_received_at_utc_ms,
            )
            .map_err(ActualAiringWriteError::Invalid)?;
        }
        let fact = ActualAiringFact::from_observation(observation, trusted_received_at_utc_ms);

        let count = self
            .count_actual_airings()
            .map_err(ActualAiringWriteError::Storage)?;
        if count >= MAX_ACTUAL_AIRINGS {
            return Err(ActualAiringWriteError::Capacity);
        }
        let schedule_item_id = fact.schedule_item_id.as_deref().unwrap_or_default();
        let schedule_item_revision = fact
            .schedule_item_revision
            .map(|value| checked_i64(value, "schedule item revision"))
            .transpose()
            .map_err(ActualAiringWriteError::Invalid)?
            .unwrap_or(-1);
        let values = [
            c_string(&fact.airing_id, "airing identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.channel_id, "channel identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.asset_sha256, "asset identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(schedule_item_id, "schedule item identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.report_id, "report identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.worker_boot_id, "worker boot identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.worker_id, "worker identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(&fact.worker_ack_id, "worker acknowledgement identity")
                .map_err(ActualAiringWriteError::Storage)?,
            c_string(
                &serde_json::to_string(&fact)
                    .map_err(|error| ActualAiringWriteError::Storage(error.to_string()))?,
                "actual airing record",
            )
            .map_err(ActualAiringWriteError::Storage)?,
        ];
        let statement = self
            .prepare(
                "INSERT INTO actual_airings \
                 (airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                  report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                  worker_id,worker_ack_id,record_json) \
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
            )
            .map_err(ActualAiringWriteError::Storage)?;
        for (index, value) in values.iter().take(4).enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)
                .map_err(ActualAiringWriteError::Storage)?;
        }
        bind_i64(self.database, statement.raw, 5, schedule_item_revision)
            .map_err(ActualAiringWriteError::Storage)?;
        bind_text(self.database, statement.raw, 6, &values[4])
            .map_err(ActualAiringWriteError::Storage)?;
        bind_i64(self.database, statement.raw, 7, fact.aired_at_utc_ms)
            .map_err(ActualAiringWriteError::Storage)?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            checked_i64(fact.media_offset_ms, "media offset")
                .map_err(ActualAiringWriteError::Invalid)?,
        )
        .map_err(ActualAiringWriteError::Storage)?;
        bind_i64(self.database, statement.raw, 9, fact.received_at_utc_ms)
            .map_err(ActualAiringWriteError::Storage)?;
        bind_text(self.database, statement.raw, 10, &values[5])
            .map_err(ActualAiringWriteError::Storage)?;
        bind_i64(
            self.database,
            statement.raw,
            11,
            checked_i64(fact.worker_sequence, "worker sequence")
                .map_err(ActualAiringWriteError::Invalid)?,
        )
        .map_err(ActualAiringWriteError::Storage)?;
        bind_text(self.database, statement.raw, 12, &values[6])
            .map_err(ActualAiringWriteError::Storage)?;
        bind_text(self.database, statement.raw, 13, &values[7])
            .map_err(ActualAiringWriteError::Storage)?;
        bind_text(self.database, statement.raw, 14, &values[8])
            .map_err(ActualAiringWriteError::Storage)?;
        expect_done(self.database, statement.raw).map_err(ActualAiringWriteError::Storage)?;
        Ok(ActualAiringWriteOutcome::Inserted)
    }

    /// Atomically accepts one worker-confirmed scheduled take.
    ///
    /// The worker supplies event identities and its actual on-air instant, but
    /// never station receive time or runtime heartbeat time. While holding one
    /// `BEGIN IMMEDIATE` transaction this method re-reads the immutable commit
    /// report and exact committed schedule-history revision, appends the as-run
    /// fact, and advances `Queued` to `Acknowledged`. It also persists the
    /// constructed `OnAir` runtime observation only while the committed
    /// schedule window remains current. Any disagreement rolls those writes
    /// back. A late worker-journal delivery is accepted only when its actual
    /// time lies inside that committed schedule window, and returns a distinct
    /// historical outcome without replacing the current runtime projection. A
    /// first-time atomic apply rejects any worker timestamp ahead of the
    /// station receive clock.
    pub fn apply_scheduled_take_acknowledgement(
        &self,
        acknowledgement: &ScheduledTakeAcknowledgement,
    ) -> Result<ScheduledTakeApplyOutcome, ScheduledTakeApplyError> {
        validate_scheduled_take_acknowledgement(acknowledgement)
            .map_err(ScheduledTakeApplyError::Invalid)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduledTakeApplyError::Storage)?;
        let outcome = (|| {
            let received_at_utc_ms = station_utc_ms().map_err(ScheduledTakeApplyError::Storage)?;
            self.apply_scheduled_take_acknowledgement_in_transaction(
                acknowledgement,
                received_at_utc_ms,
            )
        })();
        match outcome {
            Ok(value) => match self.execute("COMMIT") {
                Ok(()) => Ok(value),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ScheduledTakeApplyError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn apply_scheduled_take_acknowledgement_in_transaction(
        &self,
        acknowledgement: &ScheduledTakeAcknowledgement,
        received_at_utc_ms: i64,
    ) -> Result<ScheduledTakeApplyOutcome, ScheduledTakeApplyError> {
        let report = self
            .read_commit_report(&acknowledgement.report_id)
            .map_err(ScheduledTakeApplyError::Storage)?
            .ok_or(ScheduledTakeApplyError::NotFound)?;
        if report.channel_id != acknowledgement.channel_id
            || report.schedule_item_id != acknowledgement.schedule_item_id
            || report.schedule_item_revision != acknowledgement.schedule_item_revision
            || report.asset_id != acknowledgement.asset_sha256
        {
            return Err(ScheduledTakeApplyError::Conflict(
                "scheduled take acknowledgement does not match the immutable commit report".into(),
            ));
        }

        let committed_item = self
            .read_schedule_item_history_revision(
                &acknowledgement.schedule_item_id,
                acknowledgement.schedule_item_revision,
            )
            .map_err(ScheduledTakeApplyError::Storage)?
            .ok_or_else(|| {
                ScheduledTakeApplyError::Conflict(
                    "commit report has no exact schedule-history revision".into(),
                )
            })?;
        validate_committed_schedule_binding(&report, &committed_item, acknowledgement)
            .map_err(ScheduledTakeApplyError::Conflict)?;
        let observation = acknowledgement.as_actual_airing_observation();
        if matches!(
            report.dispatch_status,
            DispatchStatus::Acknowledged | DispatchStatus::Completed | DispatchStatus::Error
        ) {
            let existing = self
                .read_actual_airing_key("report_id", &acknowledgement.report_id)
                .map_err(ScheduledTakeApplyError::Storage)?
                .ok_or_else(|| {
                    ScheduledTakeApplyError::Conflict(
                        "resolved report has no immutable actual-airing receipt".into(),
                    )
                })?;
            if !existing.matches_observation(&observation) {
                return Err(ScheduledTakeApplyError::Conflict(
                    "resolved report is bound to a different actual-airing receipt".into(),
                ));
            }
            return Ok(ScheduledTakeApplyOutcome::AlreadyApplied);
        }
        if report.dispatch_status != DispatchStatus::Queued {
            return Err(ScheduledTakeApplyError::Conflict(
                "scheduled take acknowledgement requires a queued commit report".into(),
            ));
        }
        if acknowledgement.aired_at_utc_ms > received_at_utc_ms {
            return Err(ScheduledTakeApplyError::Invalid(
                "scheduled take actual-air time cannot lead the station receive clock".into(),
            ));
        }
        // The immutable report/history pair, not the mutable current schedule
        // row, owns the as-run fact. A successor revision may have been
        // committed while an exact worker journal acknowledgement was delayed.
        // That makes the acknowledgement historical-only; it must not make the
        // fact disappear or block every later prelaunch replay.
        let current_item_matches = self
            .read_schedule_item(&acknowledgement.schedule_item_id)
            .map_err(ScheduledTakeApplyError::Storage)?
            .is_some_and(|current_item| {
                current_item.state == ScheduleState::Committed
                    && current_item.revision == acknowledgement.schedule_item_revision
                    && current_item.channel_id == acknowledgement.channel_id
                    && current_item.asset_id == acknowledgement.asset_sha256
            });

        match self.append_actual_airing_in_transaction_with_receive_policy(
            &observation,
            received_at_utc_ms,
            false,
        ) {
            Ok(ActualAiringWriteOutcome::Inserted) => {}
            Ok(ActualAiringWriteOutcome::AlreadyRecorded) => {
                return Err(ScheduledTakeApplyError::Conflict(
                    "queued report already has an actual-airing receipt outside the atomic apply"
                        .into(),
                ));
            }
            Err(error) => return Err(map_actual_airing_apply_error(error)),
        }
        if !self
            .advance_dispatch_status(
                &acknowledgement.report_id,
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .map_err(ScheduledTakeApplyError::Storage)?
        {
            return Err(ScheduledTakeApplyError::Conflict(
                "commit report dispatch status changed during acknowledgement".into(),
            ));
        }

        let committed_window_end = committed_schedule_window_end(&committed_item)
            .map_err(ScheduledTakeApplyError::Conflict)?;
        if !current_item_matches
            || received_at_utc_ms < committed_item.starts_at_unix_ms
            || received_at_utc_ms >= committed_window_end
        {
            return Ok(ScheduledTakeApplyOutcome::AppliedHistorical);
        }

        let runtime = acknowledgement.runtime_observation(received_at_utc_ms);
        if let Some(current) = self
            .read_channel_runtime(&acknowledgement.channel_id)
            .map_err(ScheduledTakeApplyError::Storage)?
        {
            let same_started_state = current.state_started_at_unix_ms
                == runtime.state_started_at_unix_ms
                && (current.state != runtime.state
                    || current.on_air_asset_id != runtime.on_air_asset_id);
            if current.heartbeat_at_unix_ms > received_at_utc_ms
                || current.state_started_at_unix_ms > acknowledgement.aired_at_utc_ms
                || same_started_state
                || (current.heartbeat_at_unix_ms == received_at_utc_ms && current != runtime)
            {
                // Current runtime is a mutable projection. Never replace a
                // newer/different source with this acknowledgement, but do
                // retain the exact immutable as-run fact and resolved dispatch
                // CAS already written in this transaction.
                return Ok(ScheduledTakeApplyOutcome::AppliedHistorical);
            }
        }
        self.put_channel_runtime(&runtime)
            .map_err(ScheduledTakeApplyError::Storage)?;
        Ok(ScheduledTakeApplyOutcome::Applied)
    }

    fn read_schedule_item_history_revision(
        &self,
        item_id: &str,
        revision: u64,
    ) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, record_json, changed_by, occurred_at_unix_ms \
             FROM schedule_item_history WHERE item_id=?1 AND revision=?2",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(item_id, "schedule item history identity")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(revision, "schedule item history revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_history_row(statement.raw)?.item)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// Reads bounded prior-airing facts in the exact half-open UTC window.
    /// Results use S19's canonical `(asset_sha256, aired_at_utc_ms)` order and
    /// every returned row must pass indexed/JSON parity checks. The call fails
    /// rather than returning a prefix when more than `limit` facts match.
    pub fn list_actual_airings(
        &self,
        channel_id: &str,
        start_utc_ms: i64,
        end_utc_ms: i64,
        limit: usize,
    ) -> Result<Vec<ActualAiringFact>, ActualAiringReadError> {
        validate_actual_airing_query(channel_id, start_utc_ms, end_utc_ms, limit)
            .map_err(ActualAiringReadError::Invalid)?;
        self.list_actual_airings_bounded(channel_id, start_utc_ms, end_utc_ms, limit)
    }

    /// Reads the complete raw S19 authority window while retaining the
    /// station-wide append-only table bound. This is intentionally crate-only:
    /// ordinary paged callers keep the smaller public query contract, while
    /// materialization must validate every raw row before projection dedup.
    pub(crate) fn list_actual_airings_for_materialization(
        &self,
        channel_id: &str,
        start_utc_ms: i64,
        end_utc_ms: i64,
    ) -> Result<Vec<ActualAiringFact>, ActualAiringReadError> {
        validate_actual_airing_query_window(channel_id, start_utc_ms, end_utc_ms)
            .map_err(ActualAiringReadError::Invalid)?;
        self.list_actual_airings_bounded(channel_id, start_utc_ms, end_utc_ms, MAX_ACTUAL_AIRINGS)
    }

    fn list_actual_airings_bounded(
        &self,
        channel_id: &str,
        start_utc_ms: i64,
        end_utc_ms: i64,
        limit: usize,
    ) -> Result<Vec<ActualAiringFact>, ActualAiringReadError> {
        let count = self
            .count_actual_airings()
            .map_err(ActualAiringReadError::Storage)?;
        if count > MAX_ACTUAL_AIRINGS {
            return Err(ActualAiringReadError::Capacity);
        }
        let statement = self
            .prepare(
                "SELECT airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                        report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                        worker_id,worker_ack_id,record_json \
                 FROM actual_airings \
                 WHERE channel_id=?1 AND aired_at_utc_ms>=?2 AND aired_at_utc_ms<?3 \
                 ORDER BY asset_sha256, aired_at_utc_ms LIMIT ?4",
            )
            .map_err(ActualAiringReadError::Storage)?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel identity").map_err(ActualAiringReadError::Invalid)?,
        )
        .map_err(ActualAiringReadError::Storage)?;
        bind_i64(self.database, statement.raw, 2, start_utc_ms)
            .map_err(ActualAiringReadError::Storage)?;
        bind_i64(self.database, statement.raw, 3, end_utc_ms)
            .map_err(ActualAiringReadError::Storage)?;
        bind_i64(self.database, statement.raw, 4, (limit + 1) as i64)
            .map_err(ActualAiringReadError::Storage)?;
        let mut rows = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    rows.push(
                        read_actual_airing_row(statement.raw)
                            .map_err(ActualAiringReadError::Corrupt)?,
                    );
                    if rows.len() > limit {
                        return Err(ActualAiringReadError::Capacity);
                    }
                }
                SQLITE_DONE => return Ok(rows),
                _ => return Err(ActualAiringReadError::Storage(sqlite_error(self.database))),
            }
        }
    }

    fn read_actual_airing_key(
        &self,
        key: &str,
        value: &str,
    ) -> Result<Option<ActualAiringFact>, String> {
        if !matches!(key, "airing_id" | "worker_ack_id" | "report_id") {
            return Err("invalid actual airing lookup key".into());
        }
        let statement = self.prepare(&format!(
            "SELECT airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                    report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                    worker_id,worker_ack_id,record_json \
             FROM actual_airings WHERE {key}=?1"
        ))?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(value, "actual airing key")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_actual_airing_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_actual_airing_worker_sequence(
        &self,
        channel_id: &str,
        worker_boot_id: &str,
        worker_sequence: u64,
    ) -> Result<Option<ActualAiringFact>, String> {
        let statement = self.prepare(
            "SELECT airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,\
                    report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,\
                    worker_id,worker_ack_id,record_json \
             FROM actual_airings WHERE channel_id=?1 AND worker_boot_id=?2 AND worker_sequence=?3",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel identity")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(worker_boot_id, "worker boot identity")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(worker_sequence, "worker sequence")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_actual_airing_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn count_actual_airings(&self) -> Result<usize, String> {
        let statement = self.prepare("SELECT COUNT(*) FROM actual_airings")?;
        if unsafe { sqlite3_step(statement.raw) } != SQLITE_ROW {
            return Err(sqlite_error(self.database));
        }
        usize::try_from(unsafe { sqlite3_column_int64(statement.raw, 0) })
            .map_err(|_| "database returned an invalid actual airing count".into())
    }
}

impl ActualAiringFact {
    fn from_observation(observation: &ActualAiringObservation, received_at_utc_ms: i64) -> Self {
        Self {
            airing_id: observation.airing_id.clone(),
            channel_id: observation.channel_id.clone(),
            asset_sha256: observation.asset_sha256.clone(),
            schedule_item_id: observation.schedule_item_id.clone(),
            schedule_item_revision: observation.schedule_item_revision,
            report_id: observation.report_id.clone(),
            aired_at_utc_ms: observation.aired_at_utc_ms,
            media_offset_ms: observation.media_offset_ms,
            received_at_utc_ms,
            worker_boot_id: observation.worker_boot_id.clone(),
            worker_sequence: observation.worker_sequence,
            worker_id: observation.worker_id.clone(),
            worker_ack_id: observation.worker_ack_id.clone(),
        }
    }

    fn matches_observation(&self, observation: &ActualAiringObservation) -> bool {
        self.airing_id == observation.airing_id
            && self.channel_id == observation.channel_id
            && self.asset_sha256 == observation.asset_sha256
            && self.schedule_item_id == observation.schedule_item_id
            && self.schedule_item_revision == observation.schedule_item_revision
            && self.report_id == observation.report_id
            && self.aired_at_utc_ms == observation.aired_at_utc_ms
            && self.media_offset_ms == observation.media_offset_ms
            && self.worker_boot_id == observation.worker_boot_id
            && self.worker_sequence == observation.worker_sequence
            && self.worker_id == observation.worker_id
            && self.worker_ack_id == observation.worker_ack_id
    }
}

impl ScheduledTakeAcknowledgement {
    /// Builds an acknowledgement with two distinct deterministic UUID-shaped
    /// identities derived from the complete command/event binding. Length
    /// framing prevents concatenation ambiguity, and separate domain labels
    /// prevent the airing and worker-ack identities from being confused.
    pub fn from_binding(binding: ScheduledTakeAcknowledgementBinding) -> Result<Self, String> {
        let mut acknowledgement = Self {
            airing_id: "00000000-0000-0000-0000-000000000000".into(),
            report_id: binding.report_id,
            channel_id: binding.channel_id,
            schedule_item_id: binding.schedule_item_id,
            schedule_item_revision: binding.schedule_item_revision,
            asset_sha256: binding.asset_sha256,
            aired_at_utc_ms: binding.aired_at_utc_ms,
            media_offset_ms: binding.media_offset_ms,
            worker_boot_id: binding.worker_boot_id,
            worker_sequence: binding.worker_sequence,
            worker_id: binding.worker_id,
            worker_ack_id: "00000000-0000-0000-0000-000000000000".into(),
        };
        validate_actual_airing_observation(&acknowledgement.as_actual_airing_observation())?;
        acknowledgement.airing_id = derive_scheduled_take_identity(
            b"townlight/scheduled-take/airing-id/v1",
            &acknowledgement,
        );
        acknowledgement.worker_ack_id = derive_scheduled_take_identity(
            b"townlight/scheduled-take/worker-ack-id/v1",
            &acknowledgement,
        );
        validate_scheduled_take_acknowledgement(&acknowledgement)?;
        Ok(acknowledgement)
    }

    fn as_actual_airing_observation(&self) -> ActualAiringObservation {
        ActualAiringObservation {
            airing_id: self.airing_id.clone(),
            channel_id: self.channel_id.clone(),
            asset_sha256: self.asset_sha256.clone(),
            schedule_item_id: Some(self.schedule_item_id.clone()),
            schedule_item_revision: Some(self.schedule_item_revision),
            report_id: self.report_id.clone(),
            aired_at_utc_ms: self.aired_at_utc_ms,
            media_offset_ms: self.media_offset_ms,
            worker_boot_id: self.worker_boot_id.clone(),
            worker_sequence: self.worker_sequence,
            worker_id: self.worker_id.clone(),
            worker_ack_id: self.worker_ack_id.clone(),
        }
    }

    fn runtime_observation(&self, received_at_utc_ms: i64) -> ChannelRuntimeObservation {
        ChannelRuntimeObservation {
            channel_id: self.channel_id.clone(),
            state: ChannelRuntimeState::OnAir,
            on_air_asset_id: Some(self.asset_sha256.clone()),
            state_started_at_unix_ms: self.aired_at_utc_ms,
            heartbeat_at_unix_ms: received_at_utc_ms,
            detail: None,
        }
    }
}

fn derive_scheduled_take_identity(
    domain: &[u8],
    acknowledgement: &ScheduledTakeAcknowledgement,
) -> String {
    let mut digest = Sha256::new();
    update_length_framed(&mut digest, domain);
    let schedule_item_revision = acknowledgement.schedule_item_revision.to_be_bytes();
    let aired_at_utc_ms = acknowledgement.aired_at_utc_ms.to_be_bytes();
    let media_offset_ms = acknowledgement.media_offset_ms.to_be_bytes();
    let worker_sequence = acknowledgement.worker_sequence.to_be_bytes();
    for field in [
        acknowledgement.report_id.as_bytes(),
        acknowledgement.channel_id.as_bytes(),
        acknowledgement.schedule_item_id.as_bytes(),
        &schedule_item_revision,
        acknowledgement.asset_sha256.as_bytes(),
        &aired_at_utc_ms,
        &media_offset_ms,
        acknowledgement.worker_boot_id.as_bytes(),
        &worker_sequence,
        acknowledgement.worker_id.as_bytes(),
    ] {
        update_length_framed(&mut digest, field);
    }
    let mut bytes: [u8; 16] = digest.finalize()[..16]
        .try_into()
        .expect("SHA-256 always has at least 16 bytes");
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

fn update_length_framed(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value);
}

fn map_actual_airing_apply_error(error: ActualAiringWriteError) -> ScheduledTakeApplyError {
    match error {
        ActualAiringWriteError::Invalid(message) => ScheduledTakeApplyError::Invalid(message),
        ActualAiringWriteError::Conflict(message) => ScheduledTakeApplyError::Conflict(message),
        ActualAiringWriteError::TransactionRequired => ScheduledTakeApplyError::Storage(
            "scheduled take lost its required storage transaction".into(),
        ),
        ActualAiringWriteError::Capacity => ScheduledTakeApplyError::Capacity,
        ActualAiringWriteError::Storage(message) => ScheduledTakeApplyError::Storage(message),
    }
}

fn validate_scheduled_take_acknowledgement(
    acknowledgement: &ScheduledTakeAcknowledgement,
) -> Result<(), String> {
    validate_actual_airing_observation(&acknowledgement.as_actual_airing_observation())?;
    if acknowledgement.airing_id
        != derive_scheduled_take_identity(b"townlight/scheduled-take/airing-id/v1", acknowledgement)
        || acknowledgement.worker_ack_id
            != derive_scheduled_take_identity(
                b"townlight/scheduled-take/worker-ack-id/v1",
                acknowledgement,
            )
    {
        return Err(
            "scheduled take identities do not match the full acknowledgement binding".into(),
        );
    }
    Ok(())
}

fn validate_committed_schedule_binding(
    report: &CommitReport,
    item: &ScheduleItem,
    acknowledgement: &ScheduledTakeAcknowledgement,
) -> Result<(), String> {
    if item.state != ScheduleState::Committed
        || item.item_id != report.schedule_item_id
        || item.revision != report.schedule_item_revision
        || item.channel_id != report.channel_id
        || item.asset_id != report.asset_id
    {
        return Err(
            "commit report disagrees with its immutable committed schedule revision".into(),
        );
    }
    let end_utc_ms = committed_schedule_window_end(item)?;
    if acknowledgement.aired_at_utc_ms < item.starts_at_unix_ms
        || acknowledgement.aired_at_utc_ms >= end_utc_ms
    {
        return Err(
            "worker actual-air time is outside the immutable committed schedule window".into(),
        );
    }
    if acknowledgement.media_offset_ms >= item.duration_ms {
        return Err(
            "worker media offset is outside the immutable committed schedule duration".into(),
        );
    }
    Ok(())
}

fn committed_schedule_window_end(item: &ScheduleItem) -> Result<i64, String> {
    let duration_ms = i64::try_from(item.duration_ms)
        .map_err(|_| "committed schedule duration exceeds the UTC timestamp range".to_string())?;
    item.starts_at_unix_ms
        .checked_add(duration_ms)
        .ok_or_else(|| "committed schedule window overflows UTC time".to_string())
}

fn validate_actual_airing_observation(observation: &ActualAiringObservation) -> Result<(), String> {
    if !is_canonical_uuid(&observation.airing_id)
        || !is_canonical_uuid(&observation.channel_id)
        || !is_sha256_identity(&observation.asset_sha256)
        || !is_canonical_uuid(&observation.worker_ack_id)
        || !is_visible_actual_airing_report_id(&observation.report_id)
        || !is_canonical_uuid(&observation.worker_boot_id)
        || observation.worker_sequence == 0
        || observation.worker_sequence > i64::MAX as u64
        || observation.media_offset_ms > i64::MAX as u64
        || observation.worker_id.is_empty()
        || observation.worker_id.len() > 128
        || observation.worker_id.trim() != observation.worker_id
        || observation.worker_id.chars().any(char::is_control)
        || !(0..i64::MAX).contains(&observation.aired_at_utc_ms)
    {
        return Err("actual airing fact has invalid identity, worker, or UTC time".into());
    }
    match (
        &observation.schedule_item_id,
        observation.schedule_item_revision,
    ) {
        (None, None) => {}
        (Some(item_id), Some(revision))
            if is_canonical_uuid(item_id) && (1..=i64::MAX as u64).contains(&revision) => {}
        _ => return Err("schedule item identity and revision must be supplied together".into()),
    }
    Ok(())
}

fn validate_trusted_receive_time(
    aired_at_utc_ms: i64,
    trusted_received_at_utc_ms: i64,
) -> Result<(), String> {
    if !(0..i64::MAX).contains(&trusted_received_at_utc_ms)
        || aired_at_utc_ms
            > trusted_received_at_utc_ms.saturating_add(MAX_ACTUAL_AIRING_FUTURE_SKEW_MS)
        || trusted_received_at_utc_ms.saturating_sub(aired_at_utc_ms)
            > MAX_ACTUAL_AIRING_RECEIVE_DELAY_MS
    {
        return Err("actual airing time exceeds trusted receive-time skew or delay bounds".into());
    }
    Ok(())
}

fn validate_trusted_receive_time_allow_late(
    aired_at_utc_ms: i64,
    trusted_received_at_utc_ms: i64,
) -> Result<(), String> {
    if !(0..i64::MAX).contains(&trusted_received_at_utc_ms)
        || aired_at_utc_ms
            > trusted_received_at_utc_ms.saturating_add(MAX_ACTUAL_AIRING_FUTURE_SKEW_MS)
    {
        return Err("actual airing time exceeds trusted receive-time future-skew bound".into());
    }
    Ok(())
}

fn is_visible_actual_airing_report_id(value: &str) -> bool {
    (1..=200).contains(&value.len())
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn validate_actual_airing_fact(fact: &ActualAiringFact) -> Result<(), String> {
    let observation = ActualAiringObservation {
        airing_id: fact.airing_id.clone(),
        channel_id: fact.channel_id.clone(),
        asset_sha256: fact.asset_sha256.clone(),
        schedule_item_id: fact.schedule_item_id.clone(),
        schedule_item_revision: fact.schedule_item_revision,
        report_id: fact.report_id.clone(),
        aired_at_utc_ms: fact.aired_at_utc_ms,
        media_offset_ms: fact.media_offset_ms,
        worker_boot_id: fact.worker_boot_id.clone(),
        worker_sequence: fact.worker_sequence,
        worker_id: fact.worker_id.clone(),
        worker_ack_id: fact.worker_ack_id.clone(),
    };
    validate_actual_airing_observation(&observation)?;
    validate_trusted_receive_time_allow_late(fact.aired_at_utc_ms, fact.received_at_utc_ms)
}

fn station_utc_ms() -> Result<i64, String> {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "station clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_millis()).map_err(|_| "station UTC time overflowed".into())
}

fn validate_actual_airing_query(
    channel_id: &str,
    start_utc_ms: i64,
    end_utc_ms: i64,
    limit: usize,
) -> Result<(), String> {
    validate_actual_airing_query_window(channel_id, start_utc_ms, end_utc_ms)?;
    if !(1..=MAX_ACTUAL_AIRING_QUERY).contains(&limit) {
        Err("actual airing query is invalid, unbounded, or not half-open".into())
    } else {
        Ok(())
    }
}

fn validate_actual_airing_query_window(
    channel_id: &str,
    start_utc_ms: i64,
    end_utc_ms: i64,
) -> Result<(), String> {
    if !is_canonical_uuid(channel_id)
        || start_utc_ms < 0
        || end_utc_ms <= start_utc_ms
        || end_utc_ms - start_utc_ms > MAX_ACTUAL_AIRING_QUERY_WINDOW_MS
    {
        Err("actual airing query is invalid, unbounded, or not half-open".into())
    } else {
        Ok(())
    }
}

fn read_actual_airing_row(statement: *mut SqliteStatement) -> Result<ActualAiringFact, String> {
    let schedule_item_revision = match unsafe { sqlite3_column_int64(statement, 4) } {
        -1 => None,
        value if value >= 1 => Some(
            u64::try_from(value)
                .map_err(|_| "database returned invalid schedule item revision".to_string())?,
        ),
        _ => return Err("database returned invalid schedule item revision".into()),
    };
    let indexed = ActualAiringFact {
        airing_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        asset_sha256: column_text(statement, 2)?,
        schedule_item_id: match column_text(statement, 3)? {
            value if value.is_empty() => None,
            value => Some(value),
        },
        schedule_item_revision,
        report_id: column_text(statement, 5)?,
        aired_at_utc_ms: unsafe { sqlite3_column_int64(statement, 6) },
        media_offset_ms: u64::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned invalid media offset".to_string())?,
        received_at_utc_ms: unsafe { sqlite3_column_int64(statement, 8) },
        worker_boot_id: column_text(statement, 9)?,
        worker_sequence: u64::try_from(unsafe { sqlite3_column_int64(statement, 10) })
            .map_err(|_| "database returned invalid worker sequence".to_string())?,
        worker_id: column_text(statement, 11)?,
        worker_ack_id: column_text(statement, 12)?,
    };
    validate_actual_airing_fact(&indexed)?;
    let json = column_text(statement, 13)?;
    let parsed: ActualAiringFact = serde_json::from_str(&json)
        .map_err(|error| format!("database returned invalid actual airing JSON: {error}"))?;
    validate_actual_airing_fact(&parsed)?;
    if parsed != indexed {
        return Err("actual airing indexed columns disagree with record JSON".into());
    }
    Ok(indexed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(
        airing_id: &str,
        aired_at_utc_ms: i64,
        sequence: u64,
    ) -> ActualAiringObservation {
        ActualAiringObservation {
            airing_id: airing_id.into(),
            channel_id: "11111111-1111-1111-1111-111111111111".into(),
            asset_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            schedule_item_id: Some("22222222-2222-2222-2222-222222222222".into()),
            schedule_item_revision: Some(1),
            report_id: airing_id.into(),
            aired_at_utc_ms,
            media_offset_ms: 0,
            worker_boot_id: "33333333-3333-3333-3333-333333333333".into(),
            worker_sequence: sequence,
            worker_id: "transaction-test-worker".into(),
            worker_ack_id: airing_id.into(),
        }
    }

    fn probe_count(store: &StationStore) -> i64 {
        let statement = store
            .prepare("SELECT COUNT(*) FROM actual_airing_transaction_probe")
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(statement.raw) }, SQLITE_ROW);
        unsafe { sqlite3_column_int64(statement.raw, 0) }
    }

    #[test]
    fn transaction_seam_rolls_back_and_commits_with_sibling_state() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-airing-transaction-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute("CREATE TABLE actual_airing_transaction_probe (probe_id INTEGER PRIMARY KEY)")
            .unwrap();
        let now = station_utc_ms().unwrap();
        let rolled_back = observation("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", now - 100, 1);

        store.execute("BEGIN IMMEDIATE").unwrap();
        store
            .execute("INSERT INTO actual_airing_transaction_probe(probe_id) VALUES (1)")
            .unwrap();
        assert_eq!(
            store.append_actual_airing_in_transaction(&rolled_back, now),
            Ok(ActualAiringWriteOutcome::Inserted)
        );
        store.execute("ROLLBACK").unwrap();
        assert_eq!(probe_count(&store), 0);
        assert!(
            store
                .list_actual_airings(&rolled_back.channel_id, now - 1_000, now + 1_000, 1,)
                .unwrap()
                .is_empty()
        );

        let committed = observation("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", now - 50, 2);
        store.execute("BEGIN IMMEDIATE").unwrap();
        store
            .execute("INSERT INTO actual_airing_transaction_probe(probe_id) VALUES (2)")
            .unwrap();
        assert_eq!(
            store.append_actual_airing_in_transaction(&committed, now),
            Ok(ActualAiringWriteOutcome::Inserted)
        );
        store.execute("COMMIT").unwrap();
        assert_eq!(probe_count(&store), 1);
        assert_eq!(
            store
                .list_actual_airings(&committed.channel_id, now - 1_000, now + 1_000, 1,)
                .unwrap()
                .len(),
            1
        );
    }
}
