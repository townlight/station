use std::ffi::{CStr, CString, c_char, c_int, c_longlong, c_void};
use std::path::Path;
use std::ptr;

use station_domain::{StationProfile, StationProfileDocument};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestJobStatus, IngestSourceKind, LoudnessGateStatus, MediaIngestJob,
    TranscodeFormat, TranscodeJob, TranscodeJobStatus, WatchFileObservation, WatchFolderConfig,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitPlan, CommitReport, DispatchJob, DispatchStatus,
    MediaAsset, ScheduleItem, ScheduleState, prepare_commit,
};

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_OPEN_READWRITE: c_int = 0x0000_0002;
const SQLITE_OPEN_CREATE: c_int = 0x0000_0004;
const SQLITE_OPEN_FULLMUTEX: c_int = 0x0001_0000;

#[repr(C)]
struct Sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct SqliteStatement {
    _private: [u8; 0],
}

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut Sqlite3,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut Sqlite3) -> c_int;
    fn sqlite3_exec(
        database: *mut Sqlite3,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        database: *mut Sqlite3,
        sql: *const c_char,
        byte_count: c_int,
        statement: *mut *mut SqliteStatement,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_bind_text(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_char,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_bind_int64(
        statement: *mut SqliteStatement,
        index: c_int,
        value: c_longlong,
    ) -> c_int;
    fn sqlite3_step(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_column_text(statement: *mut SqliteStatement, column: c_int) -> *const u8;
    fn sqlite3_column_int64(statement: *mut SqliteStatement, column: c_int) -> c_longlong;
    fn sqlite3_finalize(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_errmsg(database: *mut Sqlite3) -> *const c_char;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProfileWriteError {
    Conflict(u64),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitWriteError {
    NotFound,
    GateFailed(Box<CommitPlan>),
    Invalid(&'static str),
    Storage(String),
}

pub struct StationStore {
    database: *mut Sqlite3,
}

impl StationStore {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        let filename = CString::new(database_path.as_ref().to_string_lossy().as_bytes())
            .map_err(|_| "database path contains a null byte".to_string())?;
        let mut database = ptr::null_mut();
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX;
        // SAFETY: `filename` is a valid C string, `database` points to writable storage, and the
        // returned handle is owned by `StationStore` until `Drop` closes it.
        let status =
            unsafe { sqlite3_open_v2(filename.as_ptr(), &mut database, flags, ptr::null()) };
        if status != SQLITE_OK {
            let message = sqlite_error(database);
            if !database.is_null() {
                // SAFETY: SQLite returned this handle and it has not been closed.
                unsafe { sqlite3_close(database) };
            }
            return Err(message);
        }

        let store = Self { database };
        store.execute("PRAGMA busy_timeout=5000;")?;
        store.execute(
            "PRAGMA journal_mode=WAL;\
             PRAGMA foreign_keys=ON;\
             CREATE TABLE IF NOT EXISTS station_profile (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               station_id TEXT NOT NULL,\
               display_name TEXT NOT NULL,\
               timezone TEXT NOT NULL,\
               revision INTEGER NOT NULL DEFAULT 1\
             );\
             CREATE TABLE IF NOT EXISTS media_assets (\
               asset_id TEXT PRIMARY KEY,\
               media_path TEXT NOT NULL,\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE TABLE IF NOT EXISTS asset_loudness_analysis (\
               asset_id TEXT PRIMARY KEY,\
               status TEXT NOT NULL CHECK (status IN ('ok','failed','not_checked')),\
               measured_lufs_milli INTEGER NOT NULL DEFAULT -2147483648,\
               target_lufs_milli INTEGER NOT NULL,\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS media_ingest_jobs_status_time \
               ON media_ingest_jobs(status, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS transcode_jobs (\
               job_id TEXT PRIMARY KEY,\
               ingest_job_id TEXT NOT NULL UNIQUE,\
               source_asset_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               output_format TEXT NOT NULL CHECK (output_format IN ('h264_720p4mbps_aac192k_ts')),\
               status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               output_asset_id TEXT NOT NULL DEFAULT '',\
               error_detail TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS transcode_jobs_status_time \
               ON transcode_jobs(status, next_attempt_at_unix_ms, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS watch_folder_configs (\
               config_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               monitor_path TEXT NOT NULL,\
               extensions_json TEXT NOT NULL,\
               settle_seconds INTEGER NOT NULL CHECK (settle_seconds BETWEEN 1 AND 3600),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               last_scan_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (last_scan_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT ''\
             );\
             CREATE TABLE IF NOT EXISTS watch_file_observations (\
               config_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),\
               modified_at_unix_ms INTEGER NOT NULL CHECK (modified_at_unix_ms >= 0),\
               unchanged_since_unix_ms INTEGER NOT NULL CHECK (unchanged_since_unix_ms >= 0),\
               observation_count INTEGER NOT NULL CHECK (observation_count >= 1),\
               queued_job_id TEXT NOT NULL DEFAULT '',\
               PRIMARY KEY(config_id, source_path),\
               FOREIGN KEY(config_id) REFERENCES watch_folder_configs(config_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS channels (\
               channel_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               udp_destination TEXT NOT NULL,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1))\
             );\
             CREATE TABLE IF NOT EXISTS schedule_items (\
               item_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               title TEXT NOT NULL,\
               starts_at_unix_ms INTEGER NOT NULL CHECK (starts_at_unix_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               state TEXT NOT NULL CHECK (state IN ('draft','committed','cancelled'))\
             );\
             CREATE INDEX IF NOT EXISTS schedule_items_channel_time \
               ON schedule_items(channel_id, starts_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS commit_reports_channel_time \
               ON commit_reports(channel_id, approved_at_unix_ms);",
        )?;
        store.migrate_ingest_retry_schedule()?;
        store.migrate_ingest_transcode_status()?;
        store.migrate_commit_report_statuses()?;
        Ok(store)
    }

    pub fn write_profile(
        &self,
        profile: &StationProfile,
        expected_revision: u64,
    ) -> Result<StationProfileDocument, ProfileWriteError> {
        self.execute("BEGIN IMMEDIATE")
            .map_err(ProfileWriteError::Storage)?;
        let outcome = (|| {
            let current = self.read_profile().map_err(ProfileWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |document| document.revision);
            if current_revision != expected_revision {
                return Err(ProfileWriteError::Conflict(current_revision));
            }
            self.upsert_profile(profile)
                .map_err(ProfileWriteError::Storage)?;
            Ok(StationProfileDocument {
                profile: profile.clone(),
                revision: current_revision + 1,
            })
        })();

        match outcome {
            Ok(document) => match self.execute("COMMIT") {
                Ok(()) => Ok(document),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ProfileWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_profile(&self) -> Result<Option<StationProfileDocument>, String> {
        let statement = self.prepare(
            "SELECT station_id, display_name, timezone, revision FROM station_profile WHERE singleton=1",
        )?;
        // SAFETY: the prepared statement is live.
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(StationProfileDocument {
                profile: StationProfile {
                    station_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    timezone: column_text(statement.raw, 2)?,
                },
                revision: u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 3) })
                    .map_err(|_| "database returned an invalid profile revision".to_string())?,
            })),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_media_asset(&self, asset: &MediaAsset) -> Result<(), String> {
        asset.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO media_assets(asset_id, media_path, duration_ms, readiness) VALUES(?1, ?2, ?3, ?4) \
             ON CONFLICT(asset_id) DO UPDATE SET media_path=excluded.media_path, duration_ms=excluded.duration_ms, readiness=excluded.readiness",
        )?;
        let asset_id = c_string(&asset.asset_id, "asset identity")?;
        let media_path = c_string(&asset.media_path, "media path")?;
        let readiness = c_string(readiness_name(asset.readiness), "asset readiness")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &media_path)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(asset.duration_ms, "asset duration")?,
        )?;
        bind_text(self.database, statement.raw, 4, &readiness)?;
        expect_done(self.database, statement.raw)
    }

    pub fn create_ingest_job(&self, job: &MediaIngestJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != IngestJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.error_detail.is_some()
            || job.asset_id.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new ingest jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO media_ingest_jobs(job_id, source_kind, source_path, status, progress_percent, \
             created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, 'pending', 0, ?4, -1, -1, '', '', 0, -1)",
        )?;
        let job_id = c_string(&job.job_id, "ingest job identity")?;
        let source_kind = c_string(
            ingest_source_kind_name(job.source_kind),
            "ingest source kind",
        )?;
        let source_path = c_string(&job.source_path, "ingest source path")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        bind_text(self.database, statement.raw, 2, &source_kind)?;
        bind_text(self.database, statement.raw, 3, &source_path)?;
        bind_i64(self.database, statement.raw, 4, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_ingest_job(&self, job_id: &str) -> Result<Option<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_ingest_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn recover_interrupted_ingest_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             error_detail='', next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_ingest_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<MediaIngestJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("ingest start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM media_ingest_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE media_ingest_jobs SET status='running', progress_percent=10, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', attempts=attempts+1, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "ingest job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("ingest job was claimed concurrently".into());
            }
            self.read_ingest_job(&job_id)?
                .ok_or_else(|| "claimed ingest job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn complete_ingest_job(
        &self,
        job_id: &str,
        asset: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 {
            return Err("ingest completion time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.put_media_asset(asset)?;
            self.put_asset_loudness(loudness)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, completed_at_unix_ms=?1, \
                 error_detail='', asset_id=?2 WHERE job_id=?3 AND status='running'",
            )?;
            let asset_id = c_string(&asset.asset_id, "asset identity")?;
            let job_id = c_string(job_id, "ingest job identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &asset_id)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_ingest_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest error detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn retry_ingest_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, error_detail=?1, asset_id='', next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest retry detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn create_transcode_job(&self, job: &TranscodeJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != TranscodeJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.output_asset_id.is_some()
            || job.error_detail.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new transcode jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO transcode_jobs(job_id, ingest_job_id, source_asset_id, source_path, \
             output_format, status, progress_percent, created_at_unix_ms, started_at_unix_ms, \
             completed_at_unix_ms, output_asset_id, error_detail, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, -1, -1, '', '', 0, -1)",
        )?;
        let values = [
            c_string(&job.job_id, "transcode job identity")?,
            c_string(&job.ingest_job_id, "transcode ingest identity")?,
            c_string(&job.source_asset_id, "transcode source identity")?,
            c_string(&job.source_path, "transcode source path")?,
            c_string(transcode_format_name(job.output_format), "transcode format")?,
        ];
        for (offset, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (offset + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_transcode_job(&self, job_id: &str) -> Result<Option<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_transcode_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_transcode_jobs(&self) -> Result<Vec<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_transcode_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn recover_interrupted_transcode_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail='interrupted before completion', \
             next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_transcode_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<TranscodeJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("transcode start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM transcode_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE transcode_jobs SET status='running', progress_percent=5, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, output_asset_id='', error_detail='', attempts=attempts+1, \
                 next_attempt_at_unix_ms=-1 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "transcode job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("transcode job was claimed concurrently".into());
            }
            self.read_transcode_job(&job_id)?
                .ok_or_else(|| "claimed transcode job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn update_transcode_progress(
        &self,
        job_id: &str,
        progress_percent: u8,
    ) -> Result<(), String> {
        if !(1..100).contains(&progress_percent) {
            return Err("running transcode progress must be between 1 and 99".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET progress_percent=?1 WHERE job_id=?2 AND status='running' \
             AND progress_percent<?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, i64::from(progress_percent))?;
        bind_text(self.database, statement.raw, 2, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or progress did not advance".into())
        }
    }

    pub fn complete_transcode_job(
        &self,
        job_id: &str,
        output_asset_id: &str,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 || !is_sha256_identity(output_asset_id) {
            return Err("transcode completion time or output identity is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='completed', progress_percent=100, \
             completed_at_unix_ms=?1, output_asset_id=?2, error_detail='', next_attempt_at_unix_ms=-1 \
             WHERE job_id=?3 AND status='running' AND started_at_unix_ms<=?1",
        )?;
        let output_asset_id = c_string(output_asset_id, "transcode output identity")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &output_asset_id)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or completion predates its start".into())
        }
    }

    pub fn fail_transcode_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2, \
             output_asset_id='', next_attempt_at_unix_ms=-1 WHERE job_id=?3 AND status='running' \
             AND started_at_unix_ms<=?1",
        )?;
        let detail = c_string(error_detail, "transcode error detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or failure predates its start".into())
        }
    }

    pub fn retry_transcode_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail=?1, next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "transcode retry detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running".into())
        }
    }

    pub fn handoff_ingest_to_transcode(&self, job: &TranscodeJob) -> Result<(), String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.create_transcode_job(job)? {
                return Err("transcode job or ingest handoff already exists".into());
            }
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='awaiting_transcode', progress_percent=60, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?1 AND status='running'",
            )?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_text(self.database, statement.raw, 1, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn complete_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        output: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        output.validate().map_err(|error| format!("{error:?}"))?;
        loudness.validate().map_err(|error| format!("{error:?}"))?;
        if output.readiness != AssetReadiness::Ready
            || loudness.asset_id != output.asset_id
            || loudness.status != LoudnessGateStatus::Ok
        {
            return Err(
                "transcode output and passing loudness identities must match and be ready".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            if job.status != TranscodeJobStatus::Running {
                return Err("transcode job is not running".into());
            }
            self.put_media_asset(output)?;
            self.put_asset_loudness(loudness)?;
            self.complete_transcode_job(transcode_job_id, &output.asset_id, completed_at_unix_ms)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, \
                 completed_at_unix_ms=?1, error_detail='', asset_id=?2, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let output_asset_id = c_string(&output.asset_id, "transcode output identity")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &output_asset_id)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            self.fail_transcode_job(transcode_job_id, completed_at_unix_ms, error_detail)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, \
                 error_detail=?2, asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let detail = c_string(error_detail, "transcode error detail")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &detail)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn put_channel(&self, channel: &ChannelConfiguration) -> Result<(), String> {
        channel.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO channels(channel_id, display_name, udp_destination, enabled) VALUES(?1, ?2, ?3, ?4) \
             ON CONFLICT(channel_id) DO UPDATE SET display_name=excluded.display_name, udp_destination=excluded.udp_destination, enabled=excluded.enabled",
        )?;
        let channel_id = c_string(&channel.channel_id, "channel identity")?;
        let display_name = c_string(&channel.display_name, "channel display name")?;
        let destination = c_string(&channel.udp_destination, "UDP destination")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &display_name)?;
        bind_text(self.database, statement.raw, 3, &destination)?;
        bind_i64(self.database, statement.raw, 4, i64::from(channel.enabled))?;
        expect_done(self.database, statement.raw)
    }

    pub fn list_channels(&self) -> Result<Vec<ChannelConfiguration>, String> {
        let statement = self.prepare(
            "SELECT channel_id, display_name, udp_destination, enabled FROM channels ORDER BY channel_id",
        )?;
        let mut channels = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => channels.push(ChannelConfiguration {
                    channel_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    udp_destination: column_text(statement.raw, 2)?,
                    enabled: unsafe { sqlite3_column_int64(statement.raw, 3) } == 1,
                }),
                SQLITE_DONE => return Ok(channels),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_media_asset(&self, asset_id: &str) -> Result<Option<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_media_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_media_assets(&self) -> Result<Vec<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets ORDER BY asset_id",
        )?;
        let mut assets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => assets.push(read_media_asset_row(statement.raw)?),
                SQLITE_DONE => return Ok(assets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_asset_loudness(&self, analysis: &AssetLoudnessAnalysis) -> Result<(), String> {
        analysis.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO asset_loudness_analysis(asset_id, status, measured_lufs_milli, \
             target_lufs_milli, detail) VALUES(?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status, \
             measured_lufs_milli=excluded.measured_lufs_milli, target_lufs_milli=excluded.target_lufs_milli, \
             detail=excluded.detail",
        )?;
        let asset_id = c_string(&analysis.asset_id, "loudness asset identity")?;
        let status = c_string(loudness_status_name(analysis.status), "loudness status")?;
        let detail = c_string(
            analysis.detail.as_deref().unwrap_or_default(),
            "loudness detail",
        )?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &status)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            analysis
                .measured_lufs_milli
                .map_or(i64::from(i32::MIN), i64::from),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::from(analysis.target_lufs_milli),
        )?;
        bind_text(self.database, statement.raw, 5, &detail)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_asset_loudness(
        &self,
        asset_id: &str,
    ) -> Result<Option<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "loudness asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_asset_loudness_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_asset_loudness(&self) -> Result<Vec<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis ORDER BY asset_id",
        )?;
        let mut analyses = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => analyses.push(read_asset_loudness_row(statement.raw)?),
                SQLITE_DONE => return Ok(analyses),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_ingest_jobs(&self) -> Result<Vec<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_ingest_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_watch_folder(&self, config: &WatchFolderConfig) -> Result<(), String> {
        config.validate().map_err(|error| format!("{error:?}"))?;
        if config.last_scan_at_unix_ms.is_some() || config.last_error.is_some() {
            return Err("watch-folder scan state is read-only".into());
        }
        let statement = self.prepare(
            "INSERT INTO watch_folder_configs(config_id, display_name, monitor_path, extensions_json, \
             settle_seconds, enabled, last_scan_at_unix_ms, last_error) VALUES(?1, ?2, ?3, ?4, ?5, ?6, -1, '') \
             ON CONFLICT(config_id) DO UPDATE SET display_name=excluded.display_name, \
             monitor_path=excluded.monitor_path, extensions_json=excluded.extensions_json, \
             settle_seconds=excluded.settle_seconds, enabled=excluded.enabled",
        )?;
        let values = [
            c_string(&config.config_id, "watch-folder identity")?,
            c_string(&config.display_name, "watch-folder display name")?,
            c_string(&config.monitor_path, "watch-folder path")?,
            c_string(
                &serde_json::to_string(&config.extensions).map_err(|error| {
                    format!("could not serialize watch-folder extensions: {error}")
                })?,
                "watch-folder extensions",
            )?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            5,
            i64::from(config.settle_seconds),
        )?;
        bind_i64(self.database, statement.raw, 6, i64::from(config.enabled))?;
        expect_done(self.database, statement.raw)
    }

    pub fn list_watch_folders(&self) -> Result<Vec<WatchFolderConfig>, String> {
        let statement = self.prepare(
            "SELECT config_id, display_name, monitor_path, extensions_json, settle_seconds, enabled, \
             last_scan_at_unix_ms, last_error FROM watch_folder_configs ORDER BY display_name, config_id",
        )?;
        let mut configs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => configs.push(read_watch_folder_row(statement.raw)?),
                SQLITE_DONE => return Ok(configs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_watch_folder_scan(
        &self,
        config_id: &str,
        scanned_at_unix_ms: i64,
        error_detail: Option<&str>,
    ) -> Result<(), String> {
        if scanned_at_unix_ms < 0
            || error_detail.is_some_and(|value| {
                value.trim().is_empty()
                    || value.len() > 4_000
                    || value.chars().any(char::is_control)
            })
        {
            return Err("watch-folder scan result is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE watch_folder_configs SET last_scan_at_unix_ms=?1, last_error=?2 WHERE config_id=?3",
        )?;
        let error = c_string(error_detail.unwrap_or_default(), "watch-folder scan error")?;
        let config_id = c_string(config_id, "watch-folder identity")?;
        bind_i64(self.database, statement.raw, 1, scanned_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &error)?;
        bind_text(self.database, statement.raw, 3, &config_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-folder configuration does not exist".into())
        }
    }

    pub fn observe_watch_file(
        &self,
        config_id: &str,
        source_path: &str,
        size_bytes: u64,
        modified_at_unix_ms: i64,
        observed_at_unix_ms: i64,
    ) -> Result<WatchFileObservation, String> {
        if source_path.trim().is_empty()
            || source_path.chars().any(char::is_control)
            || modified_at_unix_ms < 0
            || observed_at_unix_ms < 0
        {
            return Err("watch-file observation is invalid".into());
        }
        let existing = self.read_watch_file_observation(config_id, source_path)?;
        let unchanged = existing.as_ref().is_some_and(|value| {
            value.size_bytes == size_bytes && value.modified_at_unix_ms == modified_at_unix_ms
        });
        let unchanged_since = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .unchanged_since_unix_ms
        } else {
            observed_at_unix_ms
        };
        let observation_count = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .observation_count
                .saturating_add(1)
        } else {
            1
        };
        let queued_job_id = if unchanged {
            existing.and_then(|value| value.queued_job_id)
        } else {
            None
        };
        let statement = self.prepare(
            "INSERT INTO watch_file_observations(config_id, source_path, size_bytes, modified_at_unix_ms, \
             unchanged_since_unix_ms, observation_count, queued_job_id) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(config_id, source_path) DO UPDATE SET size_bytes=excluded.size_bytes, \
             modified_at_unix_ms=excluded.modified_at_unix_ms, unchanged_since_unix_ms=excluded.unchanged_since_unix_ms, \
             observation_count=excluded.observation_count, queued_job_id=excluded.queued_job_id",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        let queued = c_string(
            queued_job_id.as_deref().unwrap_or_default(),
            "watch-file job",
        )?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(size_bytes, "watch-file size")?,
        )?;
        bind_i64(self.database, statement.raw, 4, modified_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 5, unchanged_since)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(observation_count),
        )?;
        bind_text(self.database, statement.raw, 7, &queued)?;
        expect_done(self.database, statement.raw)?;
        Ok(WatchFileObservation {
            config_id: config_id.into(),
            source_path: source_path.into(),
            size_bytes,
            modified_at_unix_ms,
            unchanged_since_unix_ms: unchanged_since,
            observation_count,
            queued_job_id,
        })
    }

    pub fn mark_watch_file_queued(
        &self,
        config_id: &str,
        source_path: &str,
        job_id: &str,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "UPDATE watch_file_observations SET queued_job_id=?1 WHERE config_id=?2 AND source_path=?3 \
             AND (queued_job_id='' OR queued_job_id=?1)",
        )?;
        let values = [
            c_string(job_id, "watch-file job identity")?,
            c_string(config_id, "watch-folder identity")?,
            c_string(source_path, "watch-file path")?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-file observation was not queueable".into())
        }
    }

    fn read_watch_file_observation(
        &self,
        config_id: &str,
        source_path: &str,
    ) -> Result<Option<WatchFileObservation>, String> {
        let statement = self.prepare(
            "SELECT config_id, source_path, size_bytes, modified_at_unix_ms, unchanged_since_unix_ms, \
             observation_count, queued_job_id FROM watch_file_observations WHERE config_id=?1 AND source_path=?2",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_watch_file_observation_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        item.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO schedule_items(item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(item_id) DO UPDATE SET channel_id=excluded.channel_id, asset_id=excluded.asset_id, title=excluded.title, \
             starts_at_unix_ms=excluded.starts_at_unix_ms, duration_ms=excluded.duration_ms, state=excluded.state",
        )?;
        let values = [
            c_string(&item.item_id, "schedule item identity")?,
            c_string(&item.channel_id, "channel identity")?,
            c_string(&item.asset_id, "asset identity")?,
            c_string(&item.title, "schedule title")?,
            c_string(schedule_state_name(item.state), "schedule state")?,
        ];
        for (index, value) in values[..4].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 5, item.starts_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(item.duration_ms, "schedule duration")?,
        )?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_schedule_item(&self, item_id: &str) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE item_id=?1",
        )?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_channel_schedule(&self, channel_id: &str) -> Result<Vec<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE channel_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => return Ok(items),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_schedule_window(
        &self,
        starts_at_unix_ms: i64,
        ends_before_unix_ms: i64,
    ) -> Result<Vec<ScheduleItem>, String> {
        if starts_at_unix_ms < 0 || ends_before_unix_ms <= starts_at_unix_ms {
            return Err("schedule window is invalid".into());
        }
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE state='committed' AND starts_at_unix_ms>=?1 \
             AND starts_at_unix_ms<?2 ORDER BY starts_at_unix_ms, item_id",
        )?;
        bind_i64(self.database, statement.raw, 1, starts_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 2, ends_before_unix_ms)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => return Ok(items),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn prepare_schedule_commit(
        &self,
        plan_id: &str,
        item_id: &str,
    ) -> Result<CommitPlan, CommitWriteError> {
        let item = self
            .read_schedule_item(item_id)
            .map_err(CommitWriteError::Storage)?
            .ok_or(CommitWriteError::NotFound)?;
        let mut asset = self
            .read_media_asset(&item.asset_id)
            .map_err(CommitWriteError::Storage)?;
        if let Some(asset) = asset.as_mut()
            && asset.readiness == AssetReadiness::Ready
            && !Path::new(&asset.media_path).is_file()
        {
            asset.readiness = AssetReadiness::Missing;
        }
        let channel_items = self
            .list_channel_schedule(&item.channel_id)
            .map_err(CommitWriteError::Storage)?;
        prepare_commit(plan_id, &item, asset.as_ref(), &channel_items)
            .map_err(|error| CommitWriteError::Storage(format!("{error:?}")))
    }

    pub fn commit_schedule(
        &self,
        report_id: &str,
        plan_id: &str,
        item_id: &str,
        approved_by: &str,
        approved_at_unix_ms: i64,
        operator_notes: &str,
    ) -> Result<CommitReport, CommitWriteError> {
        if report_id.trim().is_empty()
            || plan_id.trim().is_empty()
            || approved_by.trim().is_empty()
            || approved_by.chars().any(char::is_control)
            || operator_notes.len() > 4_000
            || operator_notes.chars().any(char::is_control)
            || approved_at_unix_ms < 0
        {
            return Err(CommitWriteError::Invalid(
                "commit identity, operator, timestamp, or notes are invalid",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CommitWriteError::Storage)?;
        let outcome = (|| {
            let plan = self.prepare_schedule_commit(plan_id, item_id)?;
            if !plan.dry_run_passed {
                return Err(CommitWriteError::GateFailed(Box::new(plan)));
            }
            let item = self
                .read_schedule_item(item_id)
                .map_err(CommitWriteError::Storage)?
                .ok_or(CommitWriteError::NotFound)?;
            let report = CommitReport {
                report_id: report_id.to_string(),
                plan_id: plan_id.to_string(),
                channel_id: item.channel_id.clone(),
                schedule_item_id: item.item_id.clone(),
                asset_id: item.asset_id.clone(),
                approved_by: approved_by.to_string(),
                approved_at_unix_ms,
                dispatch_status: DispatchStatus::Pending,
                operator_notes: operator_notes.to_string(),
            };
            self.insert_commit_report(&report)
                .map_err(CommitWriteError::Storage)?;
            self.set_schedule_state(item_id, ScheduleState::Committed)
                .map_err(CommitWriteError::Storage)?;
            Ok(report)
        })();
        match outcome {
            Ok(report) => match self.execute("COMMIT") {
                Ok(()) => Ok(report),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CommitWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_commit_report(&self, report_id: &str) -> Result<Option<CommitReport>, String> {
        let statement = self.prepare(
            "SELECT report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes FROM commit_reports WHERE report_id=?1",
        )?;
        let report_id = c_string(report_id, "commit report identity")?;
        bind_text(self.database, statement.raw, 1, &report_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_commit_report_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_dispatch_jobs(&self, channel_id: &str) -> Result<Vec<DispatchJob>, String> {
        let statement = self.prepare(
            "SELECT r.report_id, r.plan_id, r.channel_id, r.schedule_item_id, r.asset_id, r.approved_by, \
             r.approved_at_unix_ms, r.dispatch_status, r.operator_notes, \
             s.item_id, s.channel_id, s.asset_id, s.title, s.starts_at_unix_ms, s.duration_ms, s.state, \
             a.asset_id, a.media_path, a.duration_ms, a.readiness \
             FROM commit_reports r JOIN schedule_items s ON s.item_id=r.schedule_item_id \
             JOIN media_assets a ON a.asset_id=r.asset_id WHERE r.channel_id=?1 \
             AND r.dispatch_status IN ('pending','queued','acknowledged') \
             ORDER BY s.starts_at_unix_ms, r.approved_at_unix_ms, r.report_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(DispatchJob {
                    report: read_commit_report_row(statement.raw)?,
                    item: ScheduleItem {
                        item_id: column_text(statement.raw, 9)?,
                        channel_id: column_text(statement.raw, 10)?,
                        asset_id: column_text(statement.raw, 11)?,
                        title: column_text(statement.raw, 12)?,
                        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 13) },
                        duration_ms: column_u64(statement.raw, 14, "schedule duration")?,
                        state: parse_schedule_state(&column_text(statement.raw, 15)?)?,
                    },
                    asset: MediaAsset {
                        asset_id: column_text(statement.raw, 16)?,
                        media_path: column_text(statement.raw, 17)?,
                        duration_ms: column_u64(statement.raw, 18, "asset duration")?,
                        readiness: parse_readiness(&column_text(statement.raw, 19)?)?,
                    },
                }),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn advance_dispatch_status(
        &self,
        report_id: &str,
        expected: DispatchStatus,
        next: DispatchStatus,
    ) -> Result<bool, String> {
        if !valid_dispatch_transition(expected, next) {
            return Err("invalid dispatch status transition".into());
        }
        let statement = self.prepare(
            "UPDATE commit_reports SET dispatch_status=?1 WHERE report_id=?2 AND dispatch_status=?3",
        )?;
        let next = c_string(dispatch_status_name(next), "next dispatch status")?;
        let report_id = c_string(report_id, "report identity")?;
        let expected = c_string(dispatch_status_name(expected), "expected dispatch status")?;
        bind_text(self.database, statement.raw, 1, &next)?;
        bind_text(self.database, statement.raw, 2, &report_id)?;
        bind_text(self.database, statement.raw, 3, &expected)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    fn insert_commit_report(&self, report: &CommitReport) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO commit_reports(report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )?;
        let values = [
            c_string(&report.report_id, "report identity")?,
            c_string(&report.plan_id, "plan identity")?,
            c_string(&report.channel_id, "channel identity")?,
            c_string(&report.schedule_item_id, "schedule item identity")?,
            c_string(&report.asset_id, "asset identity")?,
            c_string(&report.approved_by, "operator identity")?,
            c_string(
                dispatch_status_name(report.dispatch_status),
                "dispatch status",
            )?,
            c_string(&report.operator_notes, "operator notes")?,
        ];
        for (index, value) in values[..6].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 7, report.approved_at_unix_ms)?;
        bind_text(self.database, statement.raw, 8, &values[6])?;
        bind_text(self.database, statement.raw, 9, &values[7])?;
        expect_done(self.database, statement.raw)
    }

    fn set_schedule_state(&self, item_id: &str, state: ScheduleState) -> Result<(), String> {
        let statement = self.prepare("UPDATE schedule_items SET state=?1 WHERE item_id=?2")?;
        let state = c_string(schedule_state_name(state), "schedule state")?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &state)?;
        bind_text(self.database, statement.raw, 2, &item_id)?;
        expect_done(self.database, statement.raw)
    }

    fn execute(&self, sql: &str) -> Result<(), String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        // SAFETY: the database handle is live, SQL is a valid C string, and no callback is used.
        let status = unsafe {
            sqlite3_exec(
                self.database,
                sql.as_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn migrate_commit_report_statuses(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commit_reports'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("commit_reports schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'completed'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE commit_reports RENAME TO commit_reports_legacy;\
             CREATE TABLE commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             INSERT INTO commit_reports SELECT * FROM commit_reports_legacy;\
             DROP TABLE commit_reports_legacy;\
             CREATE INDEX commit_reports_channel_time ON commit_reports(channel_id, approved_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn migrate_ingest_retry_schedule(&self) -> Result<(), String> {
        if self.ingest_retry_column_exists()? {
            return Ok(());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.ingest_retry_column_exists()? {
                self.execute(
                    "ALTER TABLE media_ingest_jobs ADD COLUMN next_attempt_at_unix_ms INTEGER NOT NULL \
                     DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)",
                )?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn migrate_ingest_transcode_status(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_ingest_jobs'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("media_ingest_jobs schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'awaiting_transcode'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE media_ingest_jobs RENAME TO media_ingest_jobs_legacy;\
             CREATE TABLE media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             INSERT INTO media_ingest_jobs SELECT * FROM media_ingest_jobs_legacy;\
             DROP TABLE media_ingest_jobs_legacy;\
             CREATE INDEX media_ingest_jobs_status_time ON media_ingest_jobs(status, created_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn ingest_retry_column_exists(&self) -> Result<bool, String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('media_ingest_jobs') \
             WHERE name='next_attempt_at_unix_ms'",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok((unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn upsert_profile(&self, profile: &StationProfile) -> Result<(), String> {
        let sql = "INSERT INTO station_profile(singleton, station_id, display_name, timezone) VALUES(1, ?1, ?2, ?3) \
                   ON CONFLICT(singleton) DO UPDATE SET station_id=excluded.station_id, display_name=excluded.display_name, \
                   timezone=excluded.timezone, revision=station_profile.revision+1";
        let statement = self.prepare(sql)?;
        let values = [
            CString::new(profile.station_id.as_str()),
            CString::new(profile.display_name.as_str()),
            CString::new(profile.timezone.as_str()),
        ];
        for (offset, value) in values.iter().enumerate() {
            let value = value
                .as_ref()
                .map_err(|_| "profile contains a null byte".to_string())?;
            // SAFETY: the statement is live and each CString outlives `sqlite3_step` below.
            let status = unsafe {
                sqlite3_bind_text(
                    statement.raw,
                    (offset + 1) as c_int,
                    value.as_ptr(),
                    -1,
                    ptr::null_mut(),
                )
            };
            if status != SQLITE_OK {
                return Err(sqlite_error(self.database));
            }
        }
        // SAFETY: all parameters are bound and the statement is live.
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn prepare(&self, sql: &str) -> Result<Statement, String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        let mut statement = ptr::null_mut();
        // SAFETY: the database handle is live and the SQL is a valid C string.
        let status = unsafe {
            sqlite3_prepare_v2(
                self.database,
                sql.as_ptr(),
                -1,
                &mut statement,
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(Statement { raw: statement })
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn changes(&self) -> i64 {
        let statement = self
            .prepare("SELECT changes()")
            .expect("SELECT changes is valid");
        let status = unsafe { sqlite3_step(statement.raw) };
        debug_assert_eq!(status, SQLITE_ROW);
        unsafe { sqlite3_column_int64(statement.raw, 0) }
    }

    fn finish_transaction(&self, outcome: Result<(), String>) -> Result<(), String> {
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }
}

impl Drop for StationStore {
    fn drop(&mut self) {
        // SAFETY: `StationStore` owns this live handle and drops it exactly once.
        unsafe { sqlite3_close(self.database) };
    }
}

struct Statement {
    raw: *mut SqliteStatement,
}

impl Drop for Statement {
    fn drop(&mut self) {
        // SAFETY: `Statement` owns this live handle and drops it exactly once.
        unsafe { sqlite3_finalize(self.raw) };
    }
}

fn bind_text(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &CString,
) -> Result<(), String> {
    let status =
        unsafe { sqlite3_bind_text(statement, index, value.as_ptr(), -1, ptr::null_mut()) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_i64(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: i64,
) -> Result<(), String> {
    let status = unsafe { sqlite3_bind_int64(statement, index, value) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn expect_done(database: *mut Sqlite3, statement: *mut SqliteStatement) -> Result<(), String> {
    if unsafe { sqlite3_step(statement) } == SQLITE_DONE {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn c_string(value: &str, field: &str) -> Result<CString, String> {
    CString::new(value).map_err(|_| format!("{field} contains a null byte"))
}

fn checked_i64(value: u64, field: &str) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| format!("{field} exceeds SQLite's integer range"))
}

fn read_media_asset_row(statement: *mut SqliteStatement) -> Result<MediaAsset, String> {
    Ok(MediaAsset {
        asset_id: column_text(statement, 0)?,
        media_path: column_text(statement, 1)?,
        duration_ms: column_u64(statement, 2, "asset duration")?,
        readiness: parse_readiness(&column_text(statement, 3)?)?,
    })
}

fn read_asset_loudness_row(
    statement: *mut SqliteStatement,
) -> Result<AssetLoudnessAnalysis, String> {
    let measured = unsafe { sqlite3_column_int64(statement, 2) };
    let detail = column_text(statement, 4)?;
    let analysis = AssetLoudnessAnalysis {
        asset_id: column_text(statement, 0)?,
        status: parse_loudness_status(&column_text(statement, 1)?)?,
        measured_lufs_milli: if measured == i64::from(i32::MIN) {
            None
        } else {
            Some(
                i32::try_from(measured)
                    .map_err(|_| "database returned invalid measured loudness".to_string())?,
            )
        },
        target_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned invalid target loudness".to_string())?,
        detail: (!detail.is_empty()).then_some(detail),
    };
    analysis.validate().map_err(|error| format!("{error:?}"))?;
    Ok(analysis)
}

fn read_ingest_job_row(statement: *mut SqliteStatement) -> Result<MediaIngestJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 6) };
    let completed = unsafe { sqlite3_column_int64(statement, 7) };
    let error_detail = column_text(statement, 8)?;
    let asset_id = column_text(statement, 9)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 11) };
    Ok(MediaIngestJob {
        job_id: column_text(statement, 0)?,
        source_kind: parse_ingest_source_kind(&column_text(statement, 1)?)?,
        source_path: column_text(statement, 2)?,
        status: parse_ingest_job_status(&column_text(statement, 3)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid ingest progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        asset_id: (!asset_id.is_empty()).then_some(asset_id),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 10) })
            .map_err(|_| "database returned invalid ingest attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    })
}

fn read_transcode_job_row(statement: *mut SqliteStatement) -> Result<TranscodeJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 8) };
    let completed = unsafe { sqlite3_column_int64(statement, 9) };
    let output_asset_id = column_text(statement, 10)?;
    let error_detail = column_text(statement, 11)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 13) };
    let job = TranscodeJob {
        job_id: column_text(statement, 0)?,
        ingest_job_id: column_text(statement, 1)?,
        source_asset_id: column_text(statement, 2)?,
        source_path: column_text(statement, 3)?,
        output_format: parse_transcode_format(&column_text(statement, 4)?)?,
        status: parse_transcode_job_status(&column_text(statement, 5)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 6) })
            .map_err(|_| "database returned invalid transcode progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        output_asset_id: (!output_asset_id.is_empty()).then_some(output_asset_id),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 12) })
            .map_err(|_| "database returned invalid transcode attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    };
    job.validate().map_err(|error| format!("{error:?}"))?;
    Ok(job)
}

fn read_watch_folder_row(statement: *mut SqliteStatement) -> Result<WatchFolderConfig, String> {
    let last_scan = unsafe { sqlite3_column_int64(statement, 6) };
    let last_error = column_text(statement, 7)?;
    let config = WatchFolderConfig {
        config_id: column_text(statement, 0)?,
        display_name: column_text(statement, 1)?,
        monitor_path: column_text(statement, 2)?,
        extensions: serde_json::from_str(&column_text(statement, 3)?).map_err(|error| {
            format!("database returned invalid watch-folder extensions: {error}")
        })?,
        settle_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid watch-folder settle time".to_string())?,
        enabled: unsafe { sqlite3_column_int64(statement, 5) } == 1,
        last_scan_at_unix_ms: (last_scan >= 0).then_some(last_scan),
        last_error: (!last_error.is_empty()).then_some(last_error),
    };
    config.validate().map_err(|error| format!("{error:?}"))?;
    Ok(config)
}

fn read_watch_file_observation_row(
    statement: *mut SqliteStatement,
) -> Result<WatchFileObservation, String> {
    let queued = column_text(statement, 6)?;
    Ok(WatchFileObservation {
        config_id: column_text(statement, 0)?,
        source_path: column_text(statement, 1)?,
        size_bytes: column_u64(statement, 2, "watch-file size")?,
        modified_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        unchanged_since_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        observation_count: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned invalid watch-file observation count".to_string())?,
        queued_job_id: (!queued.is_empty()).then_some(queued),
    })
}

fn read_schedule_item_row(statement: *mut SqliteStatement) -> Result<ScheduleItem, String> {
    Ok(ScheduleItem {
        item_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        asset_id: column_text(statement, 2)?,
        title: column_text(statement, 3)?,
        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        duration_ms: column_u64(statement, 5, "schedule duration")?,
        state: parse_schedule_state(&column_text(statement, 6)?)?,
    })
}

fn read_commit_report_row(statement: *mut SqliteStatement) -> Result<CommitReport, String> {
    Ok(CommitReport {
        report_id: column_text(statement, 0)?,
        plan_id: column_text(statement, 1)?,
        channel_id: column_text(statement, 2)?,
        schedule_item_id: column_text(statement, 3)?,
        asset_id: column_text(statement, 4)?,
        approved_by: column_text(statement, 5)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        dispatch_status: parse_dispatch_status(&column_text(statement, 7)?)?,
        operator_notes: column_text(statement, 8)?,
    })
}

fn column_u64(statement: *mut SqliteStatement, column: c_int, field: &str) -> Result<u64, String> {
    u64::try_from(unsafe { sqlite3_column_int64(statement, column) })
        .map_err(|_| format!("database returned an invalid {field}"))
}

fn readiness_name(value: AssetReadiness) -> &'static str {
    match value {
        AssetReadiness::Ready => "ready",
        AssetReadiness::Missing => "missing",
        AssetReadiness::Rejected => "rejected",
        AssetReadiness::Processing => "processing",
    }
}

fn loudness_status_name(value: LoudnessGateStatus) -> &'static str {
    match value {
        LoudnessGateStatus::Ok => "ok",
        LoudnessGateStatus::Failed => "failed",
        LoudnessGateStatus::NotChecked => "not_checked",
    }
}

fn parse_loudness_status(value: &str) -> Result<LoudnessGateStatus, String> {
    match value {
        "ok" => Ok(LoudnessGateStatus::Ok),
        "failed" => Ok(LoudnessGateStatus::Failed),
        "not_checked" => Ok(LoudnessGateStatus::NotChecked),
        _ => Err(format!("database returned invalid loudness status {value}")),
    }
}

fn ingest_source_kind_name(value: IngestSourceKind) -> &'static str {
    match value {
        IngestSourceKind::OperatorPath => "operator_path",
        IngestSourceKind::WatchFolder => "watch_folder",
        IngestSourceKind::LiveFinalization => "live_finalization",
    }
}

fn parse_ingest_source_kind(value: &str) -> Result<IngestSourceKind, String> {
    match value {
        "operator_path" => Ok(IngestSourceKind::OperatorPath),
        "watch_folder" => Ok(IngestSourceKind::WatchFolder),
        "live_finalization" => Ok(IngestSourceKind::LiveFinalization),
        _ => Err(format!(
            "database returned invalid ingest source kind {value}"
        )),
    }
}

fn parse_ingest_job_status(value: &str) -> Result<IngestJobStatus, String> {
    match value {
        "pending" => Ok(IngestJobStatus::Pending),
        "running" => Ok(IngestJobStatus::Running),
        "awaiting_transcode" => Ok(IngestJobStatus::AwaitingTranscode),
        "completed" => Ok(IngestJobStatus::Completed),
        "failed" => Ok(IngestJobStatus::Failed),
        "cancelled" => Ok(IngestJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid ingest job status {value}"
        )),
    }
}

fn transcode_format_name(value: TranscodeFormat) -> &'static str {
    match value {
        TranscodeFormat::H264_720p4mbpsAac192kTs => "h264_720p4mbps_aac192k_ts",
    }
}

fn parse_transcode_format(value: &str) -> Result<TranscodeFormat, String> {
    match value {
        "h264_720p4mbps_aac192k_ts" => Ok(TranscodeFormat::H264_720p4mbpsAac192kTs),
        _ => Err(format!(
            "database returned invalid transcode format {value}"
        )),
    }
}

fn parse_transcode_job_status(value: &str) -> Result<TranscodeJobStatus, String> {
    match value {
        "pending" => Ok(TranscodeJobStatus::Pending),
        "running" => Ok(TranscodeJobStatus::Running),
        "completed" => Ok(TranscodeJobStatus::Completed),
        "failed" => Ok(TranscodeJobStatus::Failed),
        "cancelled" => Ok(TranscodeJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid transcode job status {value}"
        )),
    }
}

fn is_sha256_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn parse_readiness(value: &str) -> Result<AssetReadiness, String> {
    match value {
        "ready" => Ok(AssetReadiness::Ready),
        "missing" => Ok(AssetReadiness::Missing),
        "rejected" => Ok(AssetReadiness::Rejected),
        "processing" => Ok(AssetReadiness::Processing),
        _ => Err(format!("database returned invalid asset readiness {value}")),
    }
}

fn schedule_state_name(value: ScheduleState) -> &'static str {
    match value {
        ScheduleState::Draft => "draft",
        ScheduleState::Committed => "committed",
        ScheduleState::Cancelled => "cancelled",
    }
}

fn parse_schedule_state(value: &str) -> Result<ScheduleState, String> {
    match value {
        "draft" => Ok(ScheduleState::Draft),
        "committed" => Ok(ScheduleState::Committed),
        "cancelled" => Ok(ScheduleState::Cancelled),
        _ => Err(format!("database returned invalid schedule state {value}")),
    }
}

fn dispatch_status_name(value: DispatchStatus) -> &'static str {
    match value {
        DispatchStatus::Pending => "pending",
        DispatchStatus::Queued => "queued",
        DispatchStatus::Acknowledged => "acknowledged",
        DispatchStatus::Completed => "completed",
        DispatchStatus::Error => "error",
        DispatchStatus::Cancelled => "cancelled",
    }
}

fn parse_dispatch_status(value: &str) -> Result<DispatchStatus, String> {
    match value {
        "pending" => Ok(DispatchStatus::Pending),
        "queued" => Ok(DispatchStatus::Queued),
        "acknowledged" => Ok(DispatchStatus::Acknowledged),
        "completed" => Ok(DispatchStatus::Completed),
        "error" => Ok(DispatchStatus::Error),
        "cancelled" => Ok(DispatchStatus::Cancelled),
        _ => Err(format!("database returned invalid dispatch status {value}")),
    }
}

fn valid_dispatch_transition(expected: DispatchStatus, next: DispatchStatus) -> bool {
    matches!(
        (expected, next),
        (DispatchStatus::Pending, DispatchStatus::Queued)
            | (DispatchStatus::Pending, DispatchStatus::Error)
            | (DispatchStatus::Pending, DispatchStatus::Cancelled)
            | (DispatchStatus::Queued, DispatchStatus::Acknowledged)
            | (DispatchStatus::Queued, DispatchStatus::Error)
            | (DispatchStatus::Queued, DispatchStatus::Cancelled)
            | (DispatchStatus::Acknowledged, DispatchStatus::Completed)
            | (DispatchStatus::Acknowledged, DispatchStatus::Error)
    )
}

fn column_text(statement: *mut SqliteStatement, column: c_int) -> Result<String, String> {
    // SAFETY: caller provides a live statement positioned on a row and a valid column index.
    let value = unsafe { sqlite3_column_text(statement, column) };
    if value.is_null() {
        return Err("database returned an unexpected null value".into());
    }
    // SAFETY: SQLite returns a null-terminated UTF-8 buffer valid until the next step/finalize.
    Ok(unsafe { CStr::from_ptr(value.cast()) }
        .to_string_lossy()
        .into_owned())
}

fn sqlite_error(database: *mut Sqlite3) -> String {
    if database.is_null() {
        return "SQLite failed before creating a database handle".into();
    }
    // SAFETY: SQLite owns a null-terminated error buffer associated with this live handle.
    unsafe { CStr::from_ptr(sqlite3_errmsg(database)) }
        .to_string_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn migrates_the_pre_completion_dispatch_constraint_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO commit_reports VALUES('legacy','plan','channel','item','asset','operator',1,'acknowledged','kept');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let report = migrated.read_commit_report("legacy").unwrap().unwrap();
        assert_eq!(report.operator_notes, "kept");
        assert!(
            migrated
                .advance_dispatch_status(
                    "legacy",
                    DispatchStatus::Acknowledged,
                    DispatchStatus::Completed,
                )
                .unwrap()
        );
        assert_eq!(
            migrated
                .read_commit_report("legacy")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Completed
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn adds_retry_scheduling_to_an_existing_ingest_database_without_losing_jobs() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-ingest-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE media_ingest_jobs;\
                 CREATE TABLE media_ingest_jobs (\
                   job_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_path TEXT NOT NULL,\
                   status TEXT NOT NULL, progress_percent INTEGER NOT NULL, created_at_unix_ms INTEGER NOT NULL,\
                   started_at_unix_ms INTEGER NOT NULL, completed_at_unix_ms INTEGER NOT NULL,\
                   error_detail TEXT NOT NULL, asset_id TEXT NOT NULL, attempts INTEGER NOT NULL);\
                 INSERT INTO media_ingest_jobs VALUES(\
                   '11111111-2222-4333-8444-555555555555','watch_folder','C:\\incoming\\meeting.ts',\
                   'pending',0,1,-1,-1,'','','0');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let job = migrated
            .read_ingest_job("11111111-2222-4333-8444-555555555555")
            .unwrap()
            .unwrap();
        assert_eq!(job.source_kind, IngestSourceKind::WatchFolder);
        assert_eq!(job.next_attempt_at_unix_ms, None);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }
}
