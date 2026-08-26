//! Durable single-writer persistence for already authenticated reporting projections.
use super::*;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use station_reporting::runtime::{
    ASRUN_CHECKPOINT_VERSION, AsRunProjectionRuntime, ProjectedEntry, ProjectionWindow,
};

pub const MAX_ASRUN_EPG_PAGE_SIZE: usize = 200;
const MAX_PROJECTION_BYTES: usize = 32 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AsRunProjectionWriteError {
    Invalid(String),
    Conflict(String),
    Storage(String),
}
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct AsRunProjectionPage {
    pub channel_id: String,
    pub station_timezone: String,
    pub projection_window: Option<ProjectionWindow>,
    pub source_journal_id: String,
    pub source_file_identity: String,
    pub source_raw_sequence: u64,
    pub source_checkpoint_revision: u64,
    pub entries: Vec<ProjectedEntry>,
    /// Opaque, projection-bound continuation token. It becomes invalid if
    /// the channel's day window or durable source/checkpoint revision changes.
    pub next_cursor: Option<String>,
    pub checkpoint_sequence: u64,
}

impl StationStore {
    pub(super) fn initialize_asrun_epg_authority_schema(&self) -> Result<(), String> {
        self.execute("CREATE TABLE IF NOT EXISTS asrun_epg_projection_state (\
          channel_id TEXT PRIMARY KEY CHECK(length(channel_id)=36),\
          station_timezone TEXT NOT NULL CHECK(length(station_timezone) BETWEEN 1 AND 80),\
          projection_json TEXT NOT NULL, checkpoint_sequence INTEGER NOT NULL CHECK(checkpoint_sequence>=1),\
          checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash)=64), updated_at_utc_ms INTEGER NOT NULL CHECK(updated_at_utc_ms>=0));\
          CREATE TABLE IF NOT EXISTS asrun_epg_projection_evidence (\
          event_id TEXT PRIMARY KEY CHECK(length(event_id)=36), channel_id TEXT NOT NULL CHECK(length(channel_id)=36),\
          journal_sequence INTEGER NOT NULL CHECK(journal_sequence>=1),\
          segment_start_utc_ms INTEGER NOT NULL CHECK(segment_start_utc_ms>=0),\
          segment_end_utc_ms INTEGER NOT NULL CHECK(segment_end_utc_ms>segment_start_utc_ms),\
          record_json TEXT NOT NULL,\
          UNIQUE(channel_id,journal_sequence,segment_start_utc_ms,segment_end_utc_ms));\
          CREATE TRIGGER IF NOT EXISTS asrun_epg_evidence_no_update BEFORE UPDATE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END;\
          CREATE TRIGGER IF NOT EXISTS asrun_epg_evidence_no_delete BEFORE DELETE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END;
          CREATE TABLE IF NOT EXISTS asrun_epg_access_audit (audit_id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL CHECK(length(channel_id)=36),actor_id TEXT NOT NULL CHECK(length(actor_id)=36),action TEXT NOT NULL CHECK(action IN ('read','export_csv','export_xmltv')),occurred_at_utc_ms INTEGER NOT NULL CHECK(occurred_at_utc_ms>=0));
          CREATE TRIGGER IF NOT EXISTS asrun_epg_access_audit_no_update BEFORE UPDATE ON asrun_epg_access_audit BEGIN SELECT RAISE(ABORT,'as-run access audit is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS asrun_epg_access_audit_no_delete BEFORE DELETE ON asrun_epg_access_audit BEGIN SELECT RAISE(ABORT,'as-run access audit is append-only'); END;
          CREATE TABLE IF NOT EXISTS asrun_epg_projection_failure (channel_id TEXT PRIMARY KEY CHECK(length(channel_id)=36), detail TEXT NOT NULL CHECK(length(detail) BETWEEN 1 AND 512), first_observed_at_utc_ms INTEGER NOT NULL CHECK(first_observed_at_utc_ms>=0), last_observed_at_utc_ms INTEGER NOT NULL CHECK(last_observed_at_utc_ms>=0), occurrence_count INTEGER NOT NULL CHECK(occurrence_count>=1));")?;
        self.migrate_asrun_epg_segment_evidence()
    }

    fn migrate_asrun_epg_segment_evidence(&self) -> Result<(), String> {
        if self.sqlite_table_has_column("asrun_epg_projection_evidence", "segment_start_utc_ms")? {
            return Ok(());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let steps = [
                "DROP TRIGGER IF EXISTS asrun_epg_evidence_no_update",
                "DROP TRIGGER IF EXISTS asrun_epg_evidence_no_delete",
                "ALTER TABLE asrun_epg_projection_evidence RENAME TO asrun_epg_projection_evidence_legacy",
                "CREATE TABLE asrun_epg_projection_evidence (\
                   event_id TEXT PRIMARY KEY CHECK(length(event_id)=36),\
                   channel_id TEXT NOT NULL CHECK(length(channel_id)=36),\
                   journal_sequence INTEGER NOT NULL CHECK(journal_sequence>=1),\
                   segment_start_utc_ms INTEGER NOT NULL CHECK(segment_start_utc_ms>=0),\
                   segment_end_utc_ms INTEGER NOT NULL CHECK(segment_end_utc_ms>segment_start_utc_ms),\
                   record_json TEXT NOT NULL,\
                   UNIQUE(channel_id,journal_sequence,segment_start_utc_ms,segment_end_utc_ms))",
                "INSERT INTO asrun_epg_projection_evidence(\
                   event_id,channel_id,journal_sequence,segment_start_utc_ms,segment_end_utc_ms,record_json)\
                 SELECT legacy.\"event_id\",legacy.\"channel_id\",legacy.\"journal_sequence\",\
                   json_extract(legacy.\"record_json\",'$.started_at_unix_ms'),\
                   json_extract(legacy.\"record_json\",'$.ended_at_unix_ms'),legacy.\"record_json\"\
                 FROM asrun_epg_projection_evidence_legacy AS legacy",
                "DROP TABLE asrun_epg_projection_evidence_legacy",
                "CREATE TRIGGER asrun_epg_evidence_no_update BEFORE UPDATE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END",
                "CREATE TRIGGER asrun_epg_evidence_no_delete BEFORE DELETE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END",
            ];
            for (index, step) in steps.into_iter().enumerate() {
                self.execute(step).map_err(|error| {
                    format!(
                        "as-run interval-evidence migration step {} failed: {error}",
                        index + 1
                    )
                })?;
                if index == 2
                    && (!self.sqlite_table_has_column(
                        "asrun_epg_projection_evidence_legacy",
                        "event_id",
                    )? || !self.sqlite_table_has_column(
                        "asrun_epg_projection_evidence_legacy",
                        "record_json",
                    )?)
                {
                    return Err(
                        "as-run legacy evidence table has no readable event identity".into(),
                    );
                }
                if index == 3
                    && (!self
                        .sqlite_table_has_column("asrun_epg_projection_evidence", "event_id")?
                        || !self.sqlite_table_has_column(
                            "asrun_epg_projection_evidence",
                            "segment_start_utc_ms",
                        )?)
                {
                    return Err("as-run replacement evidence table is malformed".into());
                }
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    /// Transactionally persists a server-derived projection. Retried journal replay is
    /// accepted only when the immutable event JSON is byte-for-byte identical.
    pub fn persist_asrun_projection(
        &self,
        projection: &AsRunProjectionRuntime,
        now_utc_ms: i64,
    ) -> Result<(), AsRunProjectionWriteError> {
        validate_projection(projection, now_utc_ms).map_err(AsRunProjectionWriteError::Invalid)?;
        let serialized = serde_json::to_string(projection)
            .map_err(|e| AsRunProjectionWriteError::Storage(e.to_string()))?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(AsRunProjectionWriteError::Storage)?;
        let outcome = (|| {
            for entry in &projection.entries {
                let record = serde_json::to_string(entry)
                    .map_err(|e| AsRunProjectionWriteError::Storage(e.to_string()))?;
                let existing = self.read_asrun_evidence(&entry.event_id)?;
                if let Some(existing) = existing {
                    if existing != record {
                        return Err(AsRunProjectionWriteError::Conflict(
                            "journal event identity conflicts with immutable as-run evidence"
                                .into(),
                        ));
                    }
                    continue;
                }
                let statement = self.prepare("INSERT INTO asrun_epg_projection_evidence(event_id,channel_id,journal_sequence,segment_start_utc_ms,segment_end_utc_ms,record_json) VALUES(?1,?2,?3,?4,?5,?6)").map_err(AsRunProjectionWriteError::Storage)?;
                bind_text(
                    self.database,
                    statement.raw,
                    1,
                    &c_string(&entry.event_id, "as-run event")
                        .map_err(AsRunProjectionWriteError::Storage)?,
                )
                .map_err(AsRunProjectionWriteError::Storage)?;
                bind_text(
                    self.database,
                    statement.raw,
                    2,
                    &c_string(&projection.channel_id, "channel")
                        .map_err(AsRunProjectionWriteError::Storage)?,
                )
                .map_err(AsRunProjectionWriteError::Storage)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    3,
                    entry.journal_sequence as i64,
                )
                .map_err(AsRunProjectionWriteError::Storage)?;
                bind_i64(self.database, statement.raw, 4, entry.started_at_unix_ms)
                    .map_err(AsRunProjectionWriteError::Storage)?;
                bind_i64(self.database, statement.raw, 5, entry.ended_at_unix_ms)
                    .map_err(AsRunProjectionWriteError::Storage)?;
                bind_text(
                    self.database,
                    statement.raw,
                    6,
                    &c_string(&record, "as-run evidence")
                        .map_err(AsRunProjectionWriteError::Storage)?,
                )
                .map_err(AsRunProjectionWriteError::Storage)?;
                if unsafe { sqlite3_step(statement.raw) } != SQLITE_DONE {
                    return Err(AsRunProjectionWriteError::Storage(sqlite_error(
                        self.database,
                    )));
                }
            }
            if let Some(current) = self
                .read_asrun_projection(&projection.channel_id)
                .map_err(AsRunProjectionWriteError::Storage)?
                && current.checkpoint.journal_sequence > projection.checkpoint.journal_sequence
                && !(current.checkpoint.checkpoint_version == 0
                    && projection.checkpoint.checkpoint_version == ASRUN_CHECKPOINT_VERSION)
            {
                return Err(AsRunProjectionWriteError::Conflict(
                    "as-run checkpoint cannot move backwards".into(),
                ));
            }
            let statement = self.prepare("INSERT INTO asrun_epg_projection_state(channel_id,station_timezone,projection_json,checkpoint_sequence,checkpoint_hash,updated_at_utc_ms) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(channel_id) DO UPDATE SET station_timezone=excluded.station_timezone,projection_json=excluded.projection_json,checkpoint_sequence=excluded.checkpoint_sequence,checkpoint_hash=excluded.checkpoint_hash,updated_at_utc_ms=excluded.updated_at_utc_ms").map_err(AsRunProjectionWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(&projection.channel_id, "channel")
                    .map_err(AsRunProjectionWriteError::Storage)?,
            )
            .map_err(AsRunProjectionWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                2,
                &c_string(&projection.station_timezone, "timezone")
                    .map_err(AsRunProjectionWriteError::Storage)?,
            )
            .map_err(AsRunProjectionWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                3,
                &c_string(&serialized, "projection").map_err(AsRunProjectionWriteError::Storage)?,
            )
            .map_err(AsRunProjectionWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                projection.checkpoint.journal_sequence as i64,
            )
            .map_err(AsRunProjectionWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                5,
                &c_string(&projection.checkpoint.journal_hash, "checkpoint")
                    .map_err(AsRunProjectionWriteError::Storage)?,
            )
            .map_err(AsRunProjectionWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 6, now_utc_ms)
                .map_err(AsRunProjectionWriteError::Storage)?;
            if unsafe { sqlite3_step(statement.raw) } != SQLITE_DONE {
                return Err(AsRunProjectionWriteError::Storage(sqlite_error(
                    self.database,
                )));
            }
            Ok(())
        })();
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(AsRunProjectionWriteError::Storage(error))
                }
            },
            Err(e) => {
                let _ = self.execute("ROLLBACK");
                Err(e)
            }
        }
    }
    pub fn page_asrun_projection(
        &self,
        channel_id: &str,
        cursor: Option<&str>,
        limit: usize,
    ) -> Result<Option<AsRunProjectionPage>, String> {
        if !is_uuid(channel_id) || limit == 0 || limit > MAX_ASRUN_EPG_PAGE_SIZE {
            return Err("as-run page query is invalid or unbounded".into());
        }
        let Some(projection) = self.read_asrun_projection(channel_id)? else {
            return Ok(None);
        };
        let offset = match cursor {
            Some(cursor) => decode_page_cursor(cursor, &projection)?,
            None => 0,
        };
        let end = offset
            .checked_add(limit)
            .ok_or("as-run page cursor overflows")?
            .min(projection.entries.len());
        let next_cursor = (end < projection.entries.len())
            .then(|| encode_page_cursor(end, &projection))
            .transpose()?;
        Ok(Some(AsRunProjectionPage {
            channel_id: projection.channel_id,
            station_timezone: projection.station_timezone,
            projection_window: projection.window,
            source_journal_id: projection.checkpoint.journal_id,
            source_file_identity: projection.checkpoint.reader_file_identity,
            source_raw_sequence: projection.checkpoint.reader_raw_sequence,
            source_checkpoint_revision: projection.checkpoint.projection_revision,
            entries: projection.entries[offset..end].to_vec(),
            next_cursor,
            checkpoint_sequence: projection.checkpoint.journal_sequence,
        }))
    }
    pub fn read_asrun_projection(
        &self,
        channel_id: &str,
    ) -> Result<Option<AsRunProjectionRuntime>, String> {
        let statement = self.prepare(
            "SELECT channel_id,station_timezone,projection_json,checkpoint_sequence,checkpoint_hash,updated_at_utc_ms FROM asrun_epg_projection_state WHERE channel_id=?1",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let row_channel = column_text(statement.raw, 0)?;
                let row_timezone = column_text(statement.raw, 1)?;
                let json = column_text(statement.raw, 2)?;
                let checkpoint_sequence = unsafe { sqlite3_column_int64(statement.raw, 3) };
                let checkpoint_hash = column_text(statement.raw, 4)?;
                let updated_at = unsafe { sqlite3_column_int64(statement.raw, 5) };
                let p: AsRunProjectionRuntime = serde_json::from_str(&json)
                    .map_err(|_| "stored as-run projection is corrupt")?;
                if row_channel != channel_id
                    || p.channel_id != row_channel
                    || p.station_timezone != row_timezone
                    || checkpoint_sequence < 1
                    || p.checkpoint.journal_sequence != checkpoint_sequence as u64
                    || p.checkpoint.journal_hash != checkpoint_hash
                    || updated_at < 0
                {
                    return Err("stored as-run projection row parity check failed".into());
                }
                validate_projection(&p, updated_at)?;
                self.verify_asrun_evidence_rows(&p)?;
                Ok(Some(p))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }
    /// Immutable, time-ordered segments retained for durable as-run audit.
    /// This is intentionally separate from the current-day projection state:
    /// a source transition can legitimately create one segment on each side
    /// of a local-midnight boundary.
    pub fn list_asrun_projection_evidence(
        &self,
        channel_id: &str,
    ) -> Result<Vec<ProjectedEntry>, String> {
        if !is_uuid(channel_id) {
            return Err("as-run evidence channel is invalid".into());
        }
        let statement = self.prepare(
            "SELECT record_json FROM asrun_epg_projection_evidence WHERE channel_id=?1 ORDER BY segment_start_utc_ms,segment_end_utc_ms,event_id",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let record = column_text(statement.raw, 0)?;
                    let entry = serde_json::from_str(&record)
                        .map_err(|_| "stored as-run evidence record is corrupt")?;
                    entries.push(entry);
                }
                SQLITE_DONE => return Ok(entries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }
    pub fn audit_asrun_access(
        &self,
        channel_id: &str,
        actor_id: &str,
        action: &str,
        occurred_at_utc_ms: i64,
    ) -> Result<(), String> {
        if !is_uuid(channel_id)
            || !is_uuid(actor_id)
            || !matches!(action, "read" | "export_csv" | "export_xmltv")
            || occurred_at_utc_ms < 0
        {
            return Err("as-run access audit is invalid".into());
        }
        let statement = self.prepare("INSERT INTO asrun_epg_access_audit(channel_id,actor_id,action,occurred_at_utc_ms) VALUES(?1,?2,?3,?4)")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(actor_id, "actor")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            3,
            &c_string(action, "action")?,
        )?;
        bind_i64(self.database, statement.raw, 4, occurred_at_utc_ms)?;
        if unsafe { sqlite3_step(statement.raw) } == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }
    /// Durable, server-only recovery state for the non-timing-critical projector.
    pub fn record_asrun_projection_failure(
        &self,
        channel_id: &str,
        detail: &str,
        now: i64,
    ) -> Result<(), String> {
        if !is_uuid(channel_id)
            || detail.is_empty()
            || detail.len() > 512
            || detail.chars().any(char::is_control)
            || now < 0
        {
            return Err("as-run projection failure is invalid".into());
        }
        let statement=self.prepare("INSERT INTO asrun_epg_projection_failure(channel_id,detail,first_observed_at_utc_ms,last_observed_at_utc_ms,occurrence_count) VALUES(?1,?2,?3,?3,1) ON CONFLICT(channel_id) DO UPDATE SET detail=excluded.detail,last_observed_at_utc_ms=excluded.last_observed_at_utc_ms,occurrence_count=asrun_epg_projection_failure.occurrence_count+1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(detail, "as-run projection failure")?,
        )?;
        bind_i64(self.database, statement.raw, 3, now)?;
        if unsafe { sqlite3_step(statement.raw) } == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }
    pub fn resolve_asrun_projection_failure(&self, channel_id: &str) -> Result<(), String> {
        let statement =
            self.prepare("DELETE FROM asrun_epg_projection_failure WHERE channel_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        if unsafe { sqlite3_step(statement.raw) } == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    /// Records a persistent reporting-lane failure and routes one deduplicated
    /// durable alert through the normal outbox. This transaction is separate
    /// from the failed projection transaction by design: an invalid journal
    /// must never advance projection state merely because it can raise an
    /// operator notification.
    pub fn observe_asrun_projection_failure_and_route(
        &self,
        channel_id: &str,
        detail: &str,
        now: i64,
    ) -> Result<AlertEvent, String> {
        if !is_uuid(channel_id)
            || detail.is_empty()
            || detail.len() > 512
            || detail.chars().any(char::is_control)
            || now < 0
        {
            return Err("as-run projection failure alert is invalid".into());
        }
        let summary = "As-run/EPG projection needs operator attention.";
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            let statement = self.prepare("INSERT INTO asrun_epg_projection_failure(channel_id,detail,first_observed_at_utc_ms,last_observed_at_utc_ms,occurrence_count) VALUES(?1,?2,?3,?3,1) ON CONFLICT(channel_id) DO UPDATE SET detail=excluded.detail,last_observed_at_utc_ms=excluded.last_observed_at_utc_ms,occurrence_count=asrun_epg_projection_failure.occurrence_count+1")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(channel_id, "channel")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                2,
                &c_string(detail, "as-run projection failure")?,
            )?;
            bind_i64(self.database, statement.raw, 3, now)?;
            expect_done(self.database, statement.raw)?;

            let current =
                self.read_firing_alert(AlertConditionKind::AsRunProjectionFailure, channel_id)?;
            if let Some(current) = &current
                && current.summary == summary
                && current.detail == detail
                && current.source_section == "S23"
            {
                recorded = Some(current.clone());
                return Ok(());
            }
            let event_id = match current {
                Some(current) => current.event_id,
                None => format!(
                    "asrun-projection-failure-{channel_id}-{}",
                    self.next_alert_episode_ordinal_locked(
                        AlertConditionKind::AsRunProjectionFailure,
                        channel_id,
                    )?
                ),
            };
            recorded = Some(self.observe_alert_and_route_locked(&AlertEvent {
                event_id,
                condition: AlertConditionKind::AsRunProjectionFailure,
                severity: AlertSeverity::Warning,
                state: AlertEventState::Firing,
                resource_ref: channel_id.into(),
                summary: summary.into(),
                detail: detail.into(),
                source_section: "S23".into(),
                first_observed_at_unix_ms: now,
                last_observed_at_unix_ms: now,
                resolved_at_unix_ms: None,
                occurrence_count: 1,
                acknowledged_at_unix_ms: None,
                acknowledged_by: None,
            })?);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "as-run projection failure alert produced no event".into())
    }

    /// Resolves the projection-failure alert only after a successful durable
    /// catch-up has committed. A failed or merely attempted reader pass never
    /// clears the operator-visible condition.
    pub fn resolve_asrun_projection_failure_after_catch_up(
        &self,
        channel_id: &str,
        now: i64,
    ) -> Result<Option<AlertEvent>, String> {
        if !is_uuid(channel_id) || now < 0 {
            return Err("as-run projection recovery alert is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let mut resolved = None;
        let outcome = (|| {
            let statement =
                self.prepare("DELETE FROM asrun_epg_projection_failure WHERE channel_id=?1")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(channel_id, "channel")?,
            )?;
            expect_done(self.database, statement.raw)?;
            resolved = self.resolve_alert_locked(
                AlertConditionKind::AsRunProjectionFailure,
                channel_id,
                now,
            )?;
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        Ok(resolved)
    }
    fn read_asrun_evidence(
        &self,
        event_id: &str,
    ) -> Result<Option<String>, AsRunProjectionWriteError> {
        let statement = self
            .prepare("SELECT record_json FROM asrun_epg_projection_evidence WHERE event_id=?1")
            .map_err(AsRunProjectionWriteError::Storage)?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(event_id, "event").map_err(AsRunProjectionWriteError::Storage)?,
        )
        .map_err(AsRunProjectionWriteError::Storage)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)
                .map(Some)
                .map_err(AsRunProjectionWriteError::Storage),
            SQLITE_DONE => Ok(None),
            _ => Err(AsRunProjectionWriteError::Storage(sqlite_error(
                self.database,
            ))),
        }
    }
    fn verify_asrun_evidence_rows(
        &self,
        projection: &AsRunProjectionRuntime,
    ) -> Result<(), String> {
        for entry in &projection.entries {
            let statement = self.prepare("SELECT channel_id,journal_sequence,segment_start_utc_ms,segment_end_utc_ms,record_json FROM asrun_epg_projection_evidence WHERE event_id=?1")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(&entry.event_id, "event")?,
            )?;
            if unsafe { sqlite3_step(statement.raw) } != SQLITE_ROW {
                return Err("stored as-run evidence row is missing".into());
            }
            let channel = column_text(statement.raw, 0)?;
            let sequence = unsafe { sqlite3_column_int64(statement.raw, 1) };
            let segment_start = unsafe { sqlite3_column_int64(statement.raw, 2) };
            let segment_end = unsafe { sqlite3_column_int64(statement.raw, 3) };
            let record = column_text(statement.raw, 4)?;
            let expected = serde_json::to_string(entry).map_err(|error| error.to_string())?;
            if channel != projection.channel_id
                || sequence < 1
                || sequence as u64 != entry.journal_sequence
                || segment_start != entry.started_at_unix_ms
                || segment_end != entry.ended_at_unix_ms
                || record != expected
            {
                return Err("stored as-run evidence row parity check failed".into());
            }
        }
        Ok(())
    }
}

const PAGE_CURSOR_VERSION: u8 = 1;
const PAGE_CURSOR_BYTES: usize = 1 + 8 + 32;

fn encode_page_cursor(
    next_offset: usize,
    projection: &AsRunProjectionRuntime,
) -> Result<String, String> {
    let next_offset = u64::try_from(next_offset).map_err(|_| "as-run page cursor overflows")?;
    let mut payload = Vec::with_capacity(PAGE_CURSOR_BYTES);
    payload.push(PAGE_CURSOR_VERSION);
    payload.extend_from_slice(&next_offset.to_be_bytes());
    payload.extend_from_slice(&projection_page_binding(projection)?);
    Ok(URL_SAFE_NO_PAD.encode(payload))
}

fn decode_page_cursor(cursor: &str, projection: &AsRunProjectionRuntime) -> Result<usize, String> {
    if cursor.len() > 128
        || !cursor
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err("as-run page cursor is invalid".into());
    }
    let payload = URL_SAFE_NO_PAD
        .decode(cursor)
        .map_err(|_| "as-run page cursor is invalid")?;
    if payload.len() != PAGE_CURSOR_BYTES || payload[0] != PAGE_CURSOR_VERSION {
        return Err("as-run page cursor is invalid".into());
    }
    let offset = u64::from_be_bytes(
        payload[1..9]
            .try_into()
            .expect("fixed-size page cursor offset"),
    );
    let offset = usize::try_from(offset).map_err(|_| "as-run page cursor is invalid")?;
    if offset > projection.entries.len() || payload[9..] != projection_page_binding(projection)? {
        return Err("as-run page cursor no longer matches the current projection".into());
    }
    Ok(offset)
}

fn projection_page_binding(projection: &AsRunProjectionRuntime) -> Result<[u8; 32], String> {
    let encoded = serde_json::to_vec(projection).map_err(|error| error.to_string())?;
    let mut hash = Sha256::new();
    hash.update(b"townlight-asrun-page-cursor-v1\0");
    hash.update(&encoded);
    Ok(hash.finalize().into())
}
fn validate_projection(p: &AsRunProjectionRuntime, now: i64) -> Result<(), String> {
    if !is_uuid(&p.channel_id)
        || jiff::tz::TimeZone::get(&p.station_timezone).is_err()
        || p.entries.len() > 100_000
        || p.checkpoint.channel_id != p.channel_id
        || p.checkpoint.journal_sequence == 0
        || !sha(&p.checkpoint.journal_hash)
        || now < 0
        || !p
            .checkpoint
            .journal_id
            .strip_prefix("station-worker-")
            .is_some_and(is_uuid)
    {
        return Err("as-run projection is invalid".into());
    }
    if !matches!(
        p.checkpoint.checkpoint_version,
        0 | 2 | ASRUN_CHECKPOINT_VERSION
    ) {
        return Err("as-run checkpoint version is unsupported".into());
    }
    let v3 = p.checkpoint.checkpoint_version == ASRUN_CHECKPOINT_VERSION;
    // A newly published active segment is intentionally empty. Its durable
    // position is byte zero, but it inherits a nonzero raw sequence and the
    // sealed compaction boundary from the authenticated manifest generation.
    let empty_active_cursor = v3
        && p.checkpoint.reader_byte_offset == 0
        && p.checkpoint.reader_generation > 0
        && p.checkpoint.reader_segment_kind == 1;
    if (p.entries.is_empty()
        && (p.checkpoint.materialized_sequence != 0 || !p.checkpoint.materialized_hash.is_empty()))
        || (!p.entries.is_empty()
            && (p.checkpoint.materialized_sequence == 0 || !sha(&p.checkpoint.materialized_hash)))
        || (v3
            && p.checkpoint.reader_byte_offset == 0
            && !empty_active_cursor
            && (!p.checkpoint.reader_prefix_hash.is_empty()
                || !p.checkpoint.reader_file_identity.is_empty()
                || !p.checkpoint.reader_worker_id.is_empty()
                || !p.checkpoint.reader_channel_id.is_empty()
                || p.checkpoint.reader_raw_sequence != 0
                || p.checkpoint.reader_saw_heartbeat_summary
                || p.checkpoint.reader_saw_compaction_commit
                || p.checkpoint.reader_compaction_next_sequence != 0
                || p.checkpoint.reader_compaction_layout_version != 0
                || !p.checkpoint.reader_manifest_hash.is_empty()
                || p.checkpoint.reader_segment_kind != 0))
        || (v3
            && (p.checkpoint.reader_byte_offset > 0 || empty_active_cursor)
            && (p.checkpoint.reader_raw_sequence == 0
                || !sha(&p.checkpoint.reader_prefix_hash)
                || p.checkpoint.reader_file_identity.trim().is_empty()
                || p.checkpoint.reader_worker_id != p.checkpoint.journal_id
                || p.checkpoint.reader_channel_id != p.channel_id
                || (p.checkpoint.reader_saw_compaction_commit
                    != (p.checkpoint.reader_compaction_next_sequence != 0))
                || (p.checkpoint.reader_compaction_layout_version != 0
                    && p.checkpoint.reader_compaction_layout_version != 2)
                || (!p.checkpoint.reader_saw_compaction_commit
                    && p.checkpoint.reader_compaction_layout_version != 0)
                || (p.checkpoint.reader_saw_heartbeat_summary
                    && !p.checkpoint.reader_saw_compaction_commit)
                || p.checkpoint.reader_segment_kind > 1
                || (p.checkpoint.reader_generation > 0
                    && (!sha(&p.checkpoint.reader_manifest_hash)
                        || (p.checkpoint.reader_segment_kind == 0
                            && p.checkpoint.reader_manifest_hash.is_empty())))
                || (p.checkpoint.reader_generation == 0
                    && (!p.checkpoint.reader_manifest_hash.is_empty()
                        || p.checkpoint.reader_segment_kind != 0))))
        || (v3
            && ((p.checkpoint.source_anchor_sequence == 0)
                != p.checkpoint.source_anchor_frame_hash.is_empty()))
        || (v3
            && p.checkpoint.source_anchor_sequence > 0
            && (!sha(&p.checkpoint.source_anchor_frame_hash)
                || p.checkpoint.source_anchor_sequence > p.checkpoint.reader_raw_sequence))
    {
        return Err("as-run reader checkpoint is invalid".into());
    }
    if let Some(window) = &p.window
        && (window.local_date.len() != 10
            || !window.local_date.bytes().enumerate().all(|(index, byte)| {
                matches!(index, 4 | 7) && byte == b'-'
                    || !matches!(index, 4 | 7) && byte.is_ascii_digit()
            })
            || window.starts_at_utc_ms < 0
            || window.ends_at_utc_ms <= window.starts_at_utc_ms
            || super::station_local_day_window(&p.station_timezone, window.starts_at_utc_ms)
                .map_or(true, |expected| expected != *window))
    {
        return Err("as-run projection window is invalid".into());
    }
    if p.checkpoint.checkpoint_version == ASRUN_CHECKPOINT_VERSION
        && let Some(pending) = &p.checkpoint.pending_start
        && (pending.journal_sequence == 0
            || pending.started_at_unix_ms < 0
            || pending.started_at_unix_ms > now
            || !sha(&pending.journal_hash)
            || pending.schedule_item_id.trim().is_empty()
            || pending.schedule_item_id.len() > 240
            || pending.asset_id.len() != 64
            || !pending
                .asset_id
                .as_bytes()
                .iter()
                .all(u8::is_ascii_hexdigit)
            || pending.schedule_item_revision == 0)
    {
        return Err("as-run pending transition is invalid".into());
    }
    if let Some(pending) = &p.checkpoint.pending_start
        && (p.checkpoint.source_anchor_sequence != pending.journal_sequence
            || p.checkpoint.source_anchor_frame_hash != pending.journal_hash)
    {
        return Err("as-run pending transition is not bound to the durable event anchor".into());
    }
    let bytes = serde_json::to_vec(p).map_err(|e| e.to_string())?;
    if bytes.len() > MAX_PROJECTION_BYTES {
        return Err("as-run projection exceeds byte bound".into());
    }
    let mut prior: Option<&ProjectedEntry> = None;
    for e in &p.entries {
        if !is_uuid(&e.event_id)
            || !sha(&e.journal_hash)
            || e.started_at_unix_ms < 0
            || e.ended_at_unix_ms <= e.started_at_unix_ms
            || e.ended_at_unix_ms > now
            || e.unavailable_reason.as_deref().is_some_and(|value| {
                value.is_empty() || value.len() > 512 || value.chars().any(char::is_control)
            })
        {
            return Err("as-run entry is invalid".into());
        }
        if !matches!(e.state, station_reporting::runtime::AsRunState::Program)
            && (e.content_id.is_some()
                || e.franchise_id.is_some()
                || e.category.is_some()
                || e.title.is_some()
                || e.catalog_revision.is_some()
                || e.schedule_revision.is_some()
                || e.unavailable_reason.is_none())
        {
            return Err("non-program as-run entry carries program authority".into());
        }
        if matches!(e.state, station_reporting::runtime::AsRunState::Program)
            && (e.content_id.is_none()
                || e.franchise_id.is_none()
                || e.category.is_none()
                || e.title.is_none()
                || e.catalog_revision.is_none()
                || e.schedule_revision.is_none())
            && e.unavailable_reason.as_deref().is_none_or(str::is_empty)
        {
            return Err("program as-run entry lacks metadata and an unavailable reason".into());
        }
        if let Some(x) = prior
            && (e.journal_sequence <= x.journal_sequence
                || e.started_at_unix_ms != x.ended_at_unix_ms)
        {
            return Err("as-run projection is noncontiguous".into());
        }
        prior = Some(e)
    }
    if let Some(last) = p.entries.last()
        && (p.checkpoint.materialized_sequence != last.journal_sequence
            || p.checkpoint.materialized_hash != last.journal_hash)
    {
        return Err("as-run materialized checkpoint does not bind the projection output".into());
    }
    if p.entries.is_empty() && p.window.is_none() {
        return Err("empty as-run projection has no durable projection window".into());
    }
    Ok(())
}
fn is_uuid(v: &str) -> bool {
    v.len() == 36
        && v.as_bytes().iter().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                *b == b'-'
            } else if i == 14 {
                *b == b'4'
            } else if i == 19 {
                matches!(*b, b'8' | b'9' | b'a' | b'b')
            } else {
                b.is_ascii_digit() || matches!(*b, b'a'..=b'f')
            }
        })
}
fn sha(v: &str) -> bool {
    v.len() == 64 && v.as_bytes().iter().all(u8::is_ascii_hexdigit)
}
