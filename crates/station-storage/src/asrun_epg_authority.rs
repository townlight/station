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
          CREATE TRIGGER IF NOT EXISTS asrun_epg_evidence_no_delete BEFORE DELETE ON asrun_epg_projection_evidence BEGIN SELECT RAISE(ABORT,'as-run evidence is append-only'); END;")
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
            {
                if current.checkpoint.journal_sequence > projection.checkpoint.journal_sequence {
                    return Err(AsRunProjectionWriteError::Conflict(
                        "as-run checkpoint cannot move backwards".into(),
                    ));
                }
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
            Ok(()) => {
                self.execute("COMMIT")
                    .map_err(AsRunProjectionWriteError::Storage)?;
                Ok(())
            }
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
        let end = (offset + limit).min(projection.entries.len());
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
            "SELECT projection_json FROM asrun_epg_projection_state WHERE channel_id=?1",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "channel")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let p: AsRunProjectionRuntime = serde_json::from_str(&json)
                    .map_err(|_| "stored as-run projection is corrupt")?;
                validate_projection(&p, 0)?;
                Ok(Some(p))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
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
}
fn validate_projection(p: &AsRunProjectionRuntime, now: i64) -> Result<(), String> {
    if !is_uuid(&p.channel_id)
        || p.station_timezone.trim().is_empty()
        || p.entries.is_empty()
        || p.entries.len() > 10_000
        || p.checkpoint.channel_id != p.channel_id
        || p.checkpoint.journal_sequence != p.entries.last().unwrap().journal_sequence
        || now < 0
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
            || e.ended_at_unix_ms <= e.started_at_unix_ms
        {
            return Err("as-run entry is invalid".into());
        }
        if let Some(x) = prior {
            if e.journal_sequence != x.journal_sequence + 1
                || e.started_at_unix_ms != x.ended_at_unix_ms
            {
                return Err("as-run projection is noncontiguous".into());
            }
        }
        prior = Some(e)
    }
    Ok(())
}
fn is_uuid(v: &str) -> bool {
    v.len() == 36
        && v.as_bytes()
            .iter()
            .enumerate()
            .all(|(i, b)| matches!(i, 8 | 13 | 18 | 23) && *b == b'-' || b.is_ascii_hexdigit())
}
fn sha(v: &str) -> bool {
    v.len() == 64 && v.as_bytes().iter().all(u8::is_ascii_hexdigit)
}
