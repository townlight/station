//! Durable single-writer persistence for already authenticated reporting projections.
use super::*;
use station_reporting::runtime::{AsRunProjectionRuntime, ProjectedEntry};

pub const MAX_ASRUN_EPG_PAGE_SIZE: usize = 200;
const MAX_PROJECTION_BYTES: usize = 4 * 1024 * 1024;

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
    pub entries: Vec<ProjectedEntry>,
    pub next_offset: Option<usize>,
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
          journal_sequence INTEGER NOT NULL CHECK(journal_sequence>=1), record_json TEXT NOT NULL,\
          UNIQUE(channel_id,journal_sequence));\
          CREATE TRIGGER IF NOT EXISTS asrun_epg_evidence_no_update BEFORE UPDATE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END;\
          CREATE TRIGGER IF NOT EXISTS asrun_epg_evidence_no_delete BEFORE DELETE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END;
          CREATE TABLE IF NOT EXISTS asrun_epg_access_audit (audit_id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL CHECK(length(channel_id)=36),actor_id TEXT NOT NULL CHECK(length(actor_id)=36),action TEXT NOT NULL CHECK(action IN ('read','export_csv','export_xmltv')),occurred_at_utc_ms INTEGER NOT NULL CHECK(occurred_at_utc_ms>=0));
          CREATE TRIGGER IF NOT EXISTS asrun_epg_access_audit_no_update BEFORE UPDATE ON asrun_epg_access_audit BEGIN SELECT RAISE(ABORT,'as-run access audit is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS asrun_epg_access_audit_no_delete BEFORE DELETE ON asrun_epg_access_audit BEGIN SELECT RAISE(ABORT,'as-run access audit is append-only'); END;
          CREATE TABLE IF NOT EXISTS asrun_epg_projection_failure (channel_id TEXT PRIMARY KEY CHECK(length(channel_id)=36), detail TEXT NOT NULL CHECK(length(detail) BETWEEN 1 AND 512), first_observed_at_utc_ms INTEGER NOT NULL CHECK(first_observed_at_utc_ms>=0), last_observed_at_utc_ms INTEGER NOT NULL CHECK(last_observed_at_utc_ms>=0), occurrence_count INTEGER NOT NULL CHECK(occurrence_count>=1));")
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
                let statement = self.prepare("INSERT INTO asrun_epg_projection_evidence(event_id,channel_id,journal_sequence,record_json) VALUES(?1,?2,?3,?4)").map_err(AsRunProjectionWriteError::Storage)?;
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
                bind_text(
                    self.database,
                    statement.raw,
                    4,
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
        offset: usize,
        limit: usize,
    ) -> Result<Option<AsRunProjectionPage>, String> {
        if !is_uuid(channel_id) || limit == 0 || limit > MAX_ASRUN_EPG_PAGE_SIZE {
            return Err("as-run page query is invalid or unbounded".into());
        }
        let Some(projection) = self.read_asrun_projection(channel_id)? else {
            return Ok(None);
        };
        if offset > projection.entries.len() {
            return Err("as-run page offset is invalid".into());
        }
        let end = offset
            .checked_add(limit)
            .ok_or("as-run page offset overflows")?
            .min(projection.entries.len());
        Ok(Some(AsRunProjectionPage {
            channel_id: projection.channel_id,
            station_timezone: projection.station_timezone,
            entries: projection.entries[offset..end].to_vec(),
            next_offset: (end < projection.entries.len()).then_some(end),
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
            let statement = self.prepare("SELECT channel_id,journal_sequence,record_json FROM asrun_epg_projection_evidence WHERE event_id=?1")?;
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
            let record = column_text(statement.raw, 2)?;
            let expected = serde_json::to_string(entry).map_err(|error| error.to_string())?;
            if channel != projection.channel_id
                || sequence < 1
                || sequence as u64 != entry.journal_sequence
                || record != expected
            {
                return Err("stored as-run evidence row parity check failed".into());
            }
        }
        Ok(())
    }
}
fn validate_projection(p: &AsRunProjectionRuntime, now: i64) -> Result<(), String> {
    if !is_uuid(&p.channel_id)
        || p.station_timezone.trim().is_empty()
        || p.entries.is_empty()
        || p.entries.len() > 10_000
        || p.checkpoint.channel_id != p.channel_id
        || p.checkpoint.journal_sequence != p.entries.last().unwrap().journal_sequence
        || p.checkpoint.journal_hash != p.entries.last().unwrap().journal_hash
        || now < 0
        || !p
            .checkpoint
            .journal_id
            .strip_prefix("station-worker-")
            .is_some_and(is_uuid)
    {
        return Err("as-run projection is invalid".into());
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
